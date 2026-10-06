# TA Generator Bot: rewrite spec (rules, feedback, Telegram UX, OCR)

Audience: Claude Code, working inside the existing `TA_Generator_Bot-main` repo.
Scope: change how rules and feedback work, add pre-generation rules and worksheet-derived formats, add OCR, and cut Telegram message noise. Everything not listed here stays as is (outline parsing, planner, renderer, auth, LLM router, job queue core, templates, logo).

Working rules for you:
- Work in the phases of section 14, in order. After each phase run `pytest` and fix before moving on. Do not start a phase with red tests.
- Keep code modular and documented (module docstring + docstrings on public functions). Match the existing style (async SQLAlchemy 2.x, python-telegram-bot v21+, plain-text Telegram messages, no Markdown parse mode).
- Never silently deactivate, delete or rewrite a user's rule. Only an explicit user action may change a rule's status or text.
- If something in this spec contradicts the code in a way I did not anticipate, stop and tell me. Do not guess.

---

## 1. What is wrong today (root causes found in the code)

1. **Feedback disappears.** `services/feedback_store.py::process_feedback` calls `find_conflicts()` (an LLM call) and, for every pair it returns, sets `old.active = False`. Any false positive silently kills an earlier rule. This is the main reason a second round of feedback "removes" the first. The conflict pool for a new course-scope rule is *all* active course rules, so unrelated rules are at risk.
2. **Rules override each other.** `generator.rules_block()` puts everything into one flat `<rules>` block and labels item rules "highest priority". There is no type scope ("all labs"), and the scope (course vs item) is guessed by the LLM with no bias toward the narrow choice.
3. **Hard-coded prompts fight the rules.** `generator.TYPE_GUIDE` (e.g. labs "with starter code", assignments "with marks breakdown table") and `FORMAT_RULES` are always injected as if they were rules. A TA rule such as "labs have no starter code" contradicts them and the outcome is random.
4. **No way to set rules before generating.** The flow is plan -> generate menu. Rules only come from post-hoc feedback.
5. **Old worksheets are weak signals.** `jobs.process_item` retrieves 2 BM25 excerpts from `reference` materials and the prompt says "do NOT copy verbatim". Nothing extracts or enforces a format.
6. **Noise.** Per job: "Queued..." message, a progress message, then per item 2 docx + 1 text message, plus busy/failed/summary messages. Feedback adds "Feedback mode...", one "Noted (n)" reply per text, a rules message, and a "Regenerating..." message.
7. **No OCR.** `pdf_text.extract_pdf` reports empty pages; `materials.extract_text` and `handlers/upload._new_course` reject scanned PDFs.

---

## 2. Locked decisions (from the owner)

- Rules are **never auto-deactivated**. When a new rule truly conflicts with an existing one, the bot asks which wins and **states the reason it considers them conflicting**.
- Scopes: **course**, **type** (all labs / all tutorials / all assignments), **item**. Precedence when they overlap: item > type > course.
- Past worksheets are **PDFs**. Use both: (a) an extracted **format spec** (stored as editable type-scope rules) and (b) **style examples** attached to the prompt.
- Regeneration is **from scratch**, always under all applicable rules. It must never drop an active rule.
- Feedback on one item is a **one-off fix for that item** unless the feedback explicitly generalizes. The owner can widen it with a button.
- Telegram: **one live-updating status message** per job. Items are sent **one by one as they finish**, **no zips**.
- **Add OCR** for scanned PDFs (outlines, worksheets, slides).
- **No existing data to preserve.** Schema can change freely. Reset the DB instead of writing data migrations (section 11).
- Free-tier LLMs stay the target. Every new LLM call must go through `LLMRouter` and handle `AllProvidersBusy` by keeping state and telling the user to retry (same pattern as the existing feedback handler).

---

## 3. Data model changes (`bot/db/models.py`)

### 3.1 `FeedbackRule` (replace `active` and `superseded_by`)

| column | type | notes |
|---|---|---|
| `scope` | String(10), not null | `course` / `type` / `item` |
| `item_type` | String(20), null | set when `scope == "type"`: `lab` / `tutorial` / `assignment` |
| `item_id` | FK plan_items, null | set when `scope == "item"` (keep `ondelete=CASCADE`) |
| `status` | String(12), not null, default `active` | `active` / `pending` / `disabled` |
| `pending_reason` | String(20), null | `conflict` / `spec_review` (only while `pending`) |
| `conflicts_with` | FK feedback_rules.id, null | existing rule that clashes (only while `pending_reason == "conflict"`) |
| `conflict_reason` | Text, null | LLM's one-sentence reason, shown to the user |
| `disabled_reason` | Text, null | e.g. `replaced by #31 (you chose Keep new)`, `rejected: you kept #12`, `disabled by you` |
| `replaced_by` | FK feedback_rules.id, null | set only by an explicit user "Keep new" |
| `origin` | String(20), not null | `feedback` / `manual` / `worksheet` |
| `batch_id` | FK rule_batches.id, null | groups rules created by one user action |
| `updated_at` | DateTime tz | |

Remove `active` and `superseded_by`. Keep `rule_text`, `course_id`, `source_message_id`, `created_at`.
Invariants (enforce in code, assert in tests): `scope=item` => `item_id` set, `item_type` null; `scope=type` => `item_type` set, `item_id` null; `scope=course` => both null.

### 3.2 New `RuleBatch`

Tracks one user action that creates rules, so conflict resolution can be asynchronous and survive restarts.

`id` (String(36) uuid pk), `course_id`, `user_id`, `kind` (`feedback`/`manual`/`spec`), `item_id` (null), `chat_id`, `card_message_id` (null, int), `regen_item_id` (null, int; set for feedback batches), `resolved` (bool, default false), `created_at`.

A batch is resolved when none of its rules is `pending`. On resolution, if `regen_item_id` is set, create the regeneration job (once; guard with `resolved`).

### 3.3 `Material` additions

- `item_type` String(20), null: for `kind == "reference"` (past worksheets): `lab`/`tutorial`/`assignment`.
- `is_example` Boolean, default false: whether the file is attached to prompts as a style example.
- `ocr_pages` Integer, null: number of pages that needed OCR (for warnings only).

### 3.3b New `TypeFormat`

`(course_id, item_type)` unique; `heading_word` String(12), default `Task` for lab, `Question` otherwise. Allowed values: `Question`, `Task`, `Problem`, `Exercise` (these are the words `generator.QUESTION_RE` already accepts). This is the only part of the format that is enforced structurally (see 5.3). Everything else in a format spec is a normal rule.

### 3.4 `FeedbackMessage`
Unchanged (raw text log). Add nothing.

### 3.5 `UserState.mode` values added
`rule_add` (data: `scope`, `item_type`, `texts`), `rule_edit` (data: `rule_id`), `rule_conflict_edit` (data: `rule_id`). Existing modes unchanged.

---

## 4. Rules engine: replace `services/feedback_store.py` with `services/rules.py`

Keep `feedback_store.py` only if needed as a thin re-export during the transition; delete it by the end of phase 3. All names below live in `services/rules.py`.

### 4.1 Applicability and precedence

```python
async def applicable_rules(s, course_id, item: PlanItem) -> Applicable
# Applicable(course: list[FeedbackRule], type: list[FeedbackRule], item: list[FeedbackRule])
# Only status == "active". Order each list by id (older first).
#   course: scope == "course"
#   type:   scope == "type" and item_type == item.type
#   item:   scope == "item" and item_id == item.id
```
`pending` and `disabled` rules are never used in generation.

### 4.2 Extraction: feedback text -> atomic rules

```python
async def extract_rules(llm, raw_text: str, item_desc: str | None, item_type: str | None) -> list[NewRule]
# NewRule(text: str, scope: "item"|"type"|"course")
```
System prompt (use as written, adjust wording only if a provider chokes on it):

```
You convert a teaching assistant's free-text feedback about ONE generated course handout into atomic rules.
Return JSON: {"rules": [{"text": str, "scope": "item"|"type"|"course"}]}.
Default scope is "item": a correction to this specific handout (a wrong answer, a question that is too long or too hard, change Q3, add a topic here).
Use "type" ONLY if the feedback explicitly generalizes to all handouts of this kind ("all labs", "every lab", "from now on for labs").
Use "course" ONLY if it explicitly generalizes to everything in the course ("everywhere", "in all material", "always use Python").
When unsure, choose "item".
Each rule is ONE imperative sentence that makes sense without the original message and contains ONE requirement.
Do not invent rules the feedback does not contain. If there is no actionable instruction, return {"rules": []}.
```
`parse_rules` stays pure and unit-testable. `scope == "type"` resolves `item_type` from the item being reviewed.

### 4.3 Conflict check

```python
@dataclass
class Clash:
    new_idx: int
    existing: FeedbackRule
    reason: str
    blocking: bool   # True only when same scope-key (see below)

async def check_conflicts(llm, s, course_id, new: list[FeedbackRule]) -> tuple[list[Clash], list[tuple[int, FeedbackRule]]]
# returns (clashes, duplicates)
```
Candidate pool for a new rule = active rules that can apply together with it:
- new `item` rule on item X: item rules for X + type rules for X's type + course rules
- new `type` rule for T: type rules for T + course rules + item rules on items of type T
- new `course` rule: all active course, type and item rules

System prompt:
```
You check whether NEW rules contradict EXISTING rules for the same course material.
Two rules conflict ONLY if no single handout could satisfy both: they give mutually exclusive instructions about the same aspect.
Rules about different aspects do not conflict. A more specific rule that only narrows or adds to a general one does not conflict unless the instructions are mutually exclusive.
Also flag a NEW rule as a duplicate if an existing rule already requires the same thing.
Return JSON: {"conflicts": [{"new": int, "existing": int, "reason": str}], "duplicates": [{"new": int, "existing": int}]}.
"reason" is ONE sentence naming the exact clash: what each rule demands.
Omit anything you are not sure about.
```
Post-processing (pure function, unit-tested): drop entries with unknown ids, drop conflicts whose `reason` is empty or shorter than 10 characters, dedupe pairs.
**Blocking vs informational:**
- Same scope-key (`course`/`course`; `type`+same `item_type`; `item`+same `item_id`) => `blocking=True`. The user must decide.
- Different scopes => `blocking=False`. Precedence resolves it at generation time. Do not ask; show it in the card as a note: `For <this item/all labs> this overrides <course/type> rule #N: <reason>`.
If the candidate pool exceeds ~40 rules, split into chunks of 40 and merge results (token limits on free tiers).

### 4.4 Lifecycle

```python
async def create_batch(s, *, course_id, user_id, kind, chat_id, item_id=None, regen_item_id=None) -> RuleBatch
async def add_rules(s, llm, batch, new: list[NewRule], *, origin, item) -> AddOutcome
# 1. insert rows: status "active", scope/item fields set
# 2. check_conflicts(); duplicates -> do NOT keep the new row (delete it), report "already covered by #N"
# 3. for each blocking clash: set the NEW rule to status="pending", pending_reason="conflict", conflicts_with=existing.id, conflict_reason=reason
#    (the EXISTING rule is untouched and stays active)
# 4. non-blocking clashes: new rule stays active, returned in AddOutcome.overrides for display
# 5. commit; return AddOutcome(rules, pending, overrides, duplicates)
async def resolve_conflict(s, rule_id, choice: "old"|"new"|"both") -> Batch | None
#   old : new rule -> disabled, disabled_reason = "rejected: you kept #<old>"
#   new : old rule -> disabled, replaced_by=new.id, disabled_reason = "replaced by #<new> (you chose Keep new)"; new rule -> active
#   both: new rule -> active (user said it is not a real conflict)
#   then if the batch has no pending rules: mark resolved and return it so the caller can queue regeneration
async def edit_rule_text(s, llm, rule_id, text) -> AddOutcome   # re-runs check_conflicts on the edited rule
async def change_scope(s, llm, rule_id, scope, item_type=None, item_id=None) -> AddOutcome  # re-runs check_conflicts
async def disable_rule(s, rule_id, reason="disabled by you")
async def restore_rule(s, llm, rule_id) -> AddOutcome            # disabled -> active, with conflict check
async def delete_rule_permanently(s, rule_id)                    # only from an explicit confirm button
```
No function in this module may change another rule's `status` except `resolve_conflict(choice="new")` and the explicit user functions above. Add a unit test that greps nothing but asserts this behaviorally (see section 12).

A failed or timed-out LLM call in `check_conflicts` must NOT silently skip the check and activate the rules unchecked. On `AllProvidersBusy`, keep the user's text (existing pattern: re-enter feedback mode with the texts) and ask them to tap Done again.

---

## 5. Generation changes (`services/generator.py`, `services/jobs.py`)

### 5.1 Prompt structure (the fix for "rules override each other")

Rebuild the student and key prompts with three clearly separated layers, in this order:

1. **TECHNICAL (non-negotiable):** math delimiters (`$...$`, `$$...$$`, never `\(`/`\[`), fenced code with language tag, heading format `### {heading_word} N` numbered 1..N, no top-level `#`, output delimiters (`=====TITLE=====` etc.), no disclaimers. These exist because the validator and renderer depend on them. Made from the current `FORMAT_RULES` with `heading_word` parameterized (5.3).
2. **DEFAULTS (apply only where the TA rules are silent):** the current `TYPE_GUIDE` text, introduced with: `DEFAULT STRUCTURE (use only for aspects the TA RULES below do not cover; TA RULES always win):`. Trim anything that is a style opinion rather than a purpose statement (for example "starter code", "progressive difficulty", "marks breakdown table" become one-line defaults the rules can replace).
3. **TA RULES (authoritative):** one block, sections in precedence order, lowest first so the strongest is last and freshest:
```
<rules>
COURSE RULES:
- ...
RULES FOR ALL {TYPE}S:
- ...   (includes worksheet-derived format rules)
RULES FOR THIS ITEM ONLY (strongest; follow exactly, including specific answers):
- ...
If two rules ever disagree, the later section wins. Do not mention these rules in the output.
</rules>
```
Omit empty sections. Rules are numbered by id internally but the ids are not shown to the model.

Then style examples (5.2). Then item/course context as today.

### 5.2 Style examples

Between rules and context add, for up to `EXAMPLES_PER_TYPE` (default 2) materials with `kind="reference"`, `item_type == item.type`, `is_example=True`:
```
<style_example source="lab3_2024.pdf">
{first EXAMPLE_MAX_CHARS characters of extracted_text, default 3500}
</style_example>
STYLE EXAMPLES show layout, numbering and phrasing conventions of earlier worksheets of this type. Mirror their structure and question style. Do NOT reuse their questions, numbers, data or topics. TA RULES override the examples where they differ.
```
`jobs.process_item`: examples come from this selection, not from BM25. Keep BM25 content excerpts from `slides`, and from `reference` files of the same `item_type` that are not examples (k=2), so topic grounding still works.

### 5.3 `heading_word`

`generator.validate_student/validate_key/question_numbers` already accept Question|Task|Problem|Exercise. Make the *required* word per type come from `TypeFormat.heading_word` so the prompt, the retry note and the key-matching all use the same word. `jobs.process_item` loads it with defaults (`Task` for labs, `Question` otherwise) when no row exists.

### 5.4 Signature changes

`generate_item(llm, course, item, neighbours, excerpts, examples, applicable: Applicable, heading_word)`.
`add_version(..., rule_ids=[...])` keeps snapshotting the ids of all applied rules (already exists as `feedback_rule_ids`). Show the count on the delivered item (section 10).

Regeneration always uses `applicable_rules()` fresh from the DB. There is no code path that regenerates with a subset of active rules.

---

## 6. Feedback flow (`handlers/feedback.py`)

State machine (all state in `UserState`, DB-backed, restart-safe):

1. **Start** (button `fb:<item_id>` or reply-to an item message). Send ONE message: `Feedback on <Lab 4>. Send as many messages as you like, then tap Done.` with buttons `[Done] [Cancel]`. Keep its `message_id` in state data.
2. **Each incoming text:** append to `texts`. Do NOT send a reply. React to the user's message with a thumbs-up via `bot.set_message_reaction` (wrap in try/except; ignore failures). Edit the one collecting message to `... (3 messages received)` through the throttled editor (10.1).
3. **Cancel:** clear state, edit the message to `Feedback cancelled. Nothing changed.`
4. **Done** (or the existing 120 s idle sweep): close input first (existing pattern), edit the collecting message to `Reading your feedback...`, then:
   - `extract_rules` -> `add_rules` inside a new `RuleBatch(kind="feedback", regen_item_id=item.id)`.
   - Edit the same message into the **result card** (below).
   - If no pending conflicts: create the regeneration job immediately (origin `feedback`).
   - If pending conflicts: do not create the job. It is created by `resolve_conflict` when the last pending rule of the batch is resolved.
5. If the item was `approved`, regeneration sends it back to `draft` (existing behavior; keep the one-line note in the card).

### 6.1 Result card (plain text, one message, edited in place)

No conflicts:
```
Saved 2 rules from your feedback:
#41 [this item] Question 3 answer must be 42.
#42 [this item] Shorten question 2 to one sentence.
Note: for this item, #42 overrides course rule #7 (reason: ...).      <- only if non-blocking clash
Already covered by #12: "Use C++ ..."                                  <- only if duplicate
Regenerating Lab 4 with all rules (2 course, 3 labs, 2 this item).
```
Buttons per new rule (rows): `[#41: all labs] [#41: whole course] [Undo #41]`. Widening calls `change_scope` (re-checks conflicts; if it creates a pending conflict, show the conflict card). Widening after regeneration started says `applies from the next generation`.

With conflicts (blocking), one message listing each:
```
#43 conflicts with #12. Reason: <conflict_reason>
  New #43: "<text>"
  Existing #12: "<text>"
```
Buttons per conflict: `[Keep #12] [Keep #43] [Both are fine] [Edit #43]`. `Edit` sets `rule_conflict_edit` mode; the next text replaces the new rule's text and re-runs the check. Header line: `Regeneration starts after you decide.`
Callback data must stay under 64 bytes: `cf:<rule_id>:<old|new|both|edit>`, `sc:<rule_id>:<type|course>`, `un:<rule_id>`.

### 6.2 Guarantees (each needs a test)
- Two successive feedback rounds on the same item leave both rules active.
- Feedback with a later contradictory rule never changes the earlier rule until the user clicks Keep new.
- A rule on item A never appears in item B's prompt. A lab-type rule never appears in a tutorial prompt.

---

## 7. Rules hub and the pre-generation gate

### 7.1 `/rules` (replace `show_rules`, `cb_rule`)

Hub message with counts and buttons: `[Course (n)] [Labs (n)] [Tutorials (n)] [Assignments (n)] [Items (n)] [Disabled (n)] [Examples] [Add rule]`. A `Pending decisions (n)` button appears only when n > 0 and opens the conflict cards.
Each group page: 8 rules per page, `#id text` plus per-rule buttons `[Edit] [Disable] [Scope]`. Disabled page: `[Restore] [Delete forever]` (the second asks for a confirm button). Rules with `origin == "worksheet"` are tagged `(from worksheets)`.
Examples page: for each item type, the worksheets with a toggle `[x] lab3_2024.pdf`; max `EXAMPLES_PER_TYPE` on; toggling a third off-then-on swaps the oldest.

### 7.2 Add rule (`rule_add` mode)

`[Add rule]` -> choose scope: `[Whole course] [All labs] [All tutorials] [All assignments]` (item scope is only created from feedback or from the Scope button on an item). Then: `Send the rule(s). Each message can contain several requirements. Tap Done when finished.` -> on Done: LLM atomizes (reuse the extraction prompt with a fixed scope: add a `fixed_scope` parameter that skips scope guessing) -> `add_rules(kind="manual")` -> same card as 6.1 minus the regeneration line, plus conflict cards if needed.

### 7.3 Gate after plan confirmation

`plan.cb_confirm` currently calls `generate.show_menu`. Change it to send a **rules gate** message instead:
```
Plan confirmed. Set rules before generating (optional).
Rules in effect: course 2 | labs 3 | tutorials 0 | assignments 1
Worksheet formats: labs yes | tutorials no | assignments no
```
Buttons: `[Add a rule] [Build format from worksheets] [Review rules] [Continue to generate]`. `Continue` opens the existing generate menu. If there are pending conflicts, `Continue` is replaced by `Resolve pending (n)` and generation stays blocked until they are resolved (`generate.cb_generate` must also refuse with a short message while any rule in the course is `pending`).
The generate menu text gets one extra line with the same "Rules in effect" summary.

---

## 8. Worksheets -> format spec + style examples

### 8.1 Upload

Replace the `up:reference` ("Past labs/tutorials") path. After the user picks it, ask the type: `[Labs] [Tutorials] [Assignments]` (callback `up:ref:<type>`). State keeps `upload_kind="reference"` and `upload_item_type`. Each saved PDF gets `item_type`; the first `EXAMPLES_PER_TYPE` uploads of a type get `is_example=True`. Zips keep working (`materials.read_zip`).
Reduce noise: while the user sends several files, keep ONE confirmation message and edit it: `Added 3 worksheets for labs (1 scanned, OCR used). [Build format spec] [Add more] [Done]`. Same OCR/size-cap warnings as today, merged into that message.

### 8.2 Build format spec (`[Build format from worksheets]` -> pick type, or button in 8.1)

Inline (not a queued job), one status message, handle `AllProvidersBusy` by telling the user to retry. Input: text of up to 3 worksheets of that type, each capped at 8,000 characters. System prompt:
```
You analyze previous worksheets of one type from a university course and describe ONLY their format and style, never their subject matter.
Return JSON: {"heading_word": "Question"|"Task"|"Problem"|"Exercise", "rules": [{"text": str, "aspect": str}]}.
"heading_word" is the word the worksheets use for a top-level numbered item (closest match of the four).
"aspect" is one of: numbering, question_style, structure, marks, difficulty, code, math, answer_key, length, other.
Write at most 12 rules. Each rule is ONE imperative sentence that is concrete and checkable (what to include, in what order, how a question is phrased, how marks are shown).
Describe patterns shared by the worksheets. If they disagree on something, omit it. Do not mention topics, specific numbers or specific questions.
```
Store results as rules: `scope="type"`, `item_type=<type>`, `origin="worksheet"`, `status="pending"`, `pending_reason="spec_review"`, in a `RuleBatch(kind="spec")`. Store `heading_word` in `TypeFormat`.

### 8.3 Review and approve

Show the proposed spec in one message (numbered, grouped by aspect) with buttons `[Approve all] [Edit...] [Delete...] [Cancel]`. `Edit`/`Delete` open per-rule buttons. On `Approve all`: run `check_conflicts` for the spec rules against existing active type and course rules. Blocking clashes become normal conflict cards (6.1). Non-clashing rules become `active`. If an earlier worksheet-origin spec exists for this type, ask `[Replace previous spec] [Keep both]` first; Replace sets the old worksheet-origin rules to `disabled` (reason `replaced by new worksheet spec`) which is an explicit user action, never automatic.

---

## 9. OCR

Add OCR fallback for pages with no text layer (outline, slides, worksheets).

- `requirements.txt`: add `pytesseract`, `Pillow`.
- `Dockerfile`: `apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng` plus a build arg `OCR_LANG_PACKAGES` (default `tesseract-ocr-eng`; example override `tesseract-ocr-eng tesseract-ocr-ara`).
- `Config`: `ocr_enabled` (`OCR_ENABLED`, default `1`), `ocr_langs` (`OCR_LANGS`, default `eng`, tesseract format like `eng+ara`), `ocr_dpi` (`OCR_DPI`, default `200`), `ocr_max_pages` (`OCR_MAX_PAGES`, default `60`). Add them to `.env.example`.
- `pdf_text.extract_pdf(data, include_tables=False, ocr=None)`: if `ocr` options are given, for each empty page render a pixmap at `ocr_dpi` with pymupdf, convert to a `PIL.Image`, run `pytesseract.image_to_string(img, lang=ocr_langs)`. Process one page at a time (memory on small hosts). Respect `ocr_max_pages` and tell the user when pages were skipped for that reason. Return `Extraction.ocr_pages: list[int]`. Treat OCR text exactly like normal text afterwards.
- It is blocking: call the OCR path through `asyncio.to_thread` from the handlers.
- Remove the "PDF has no text layer ... OCR is not supported" rejections in `materials.extract_text` and `handlers/upload._new_course`. Only fail if OCR is disabled, tesseract is missing (`pytesseract.TesseractNotFoundError`), or OCR returns (almost) nothing; the message must say which.
- Progress: for >3 OCR pages, edit one status message (`Reading scanned pages 4/12...`), throttled (10.1).
- Warning shown to the user whenever OCR was used: `N page(s) were read with OCR; check the result, equations and tables may be wrong.` The outline is already shown for review, so outline errors are catchable. Store `Material.ocr_pages`.

---

## 10. Telegram UX

### 10.1 Throttled editor

New helper in `handlers/ui.py`:
```python
class LiveMessage:
    """One message that is edited in place. Coalesces edits; min interval STATUS_EDIT_MIN_INTERVAL_S (default 3.0)."""
    async def update(self, text, reply_markup=None, force=False)
```
Swallow `BadRequest: message is not modified`; on `RetryAfter` sleep that long and retry once; never raise into the job loop. Persist `message_id` + `chat_id` in the job payload (rename `progress_msg_id` -> `status_msg_id` or keep the name; be consistent) so a restarted job keeps editing the same message.

### 10.2 One status message per job

Created at queue time (replaces "Queued N item(s). Starting shortly..."). Content, edited in place:
```
Generating labs: 3/8
Lab 1 done | Lab 2 done | Lab 3 writing... | Lab 4 | Lab 5 | ...
Rules in use: 2 course, 3 labs
```
If more than 12 items, collapse: `done 5 | failed 0 | now: Lab 6 | left: 2`.
Busy/rate-limit notices and per-item failures go INTO this message as a line (`Waiting for AI providers, resuming in ~40 s`; `Lab 5 failed: <short reason>`), not as new messages.

### 10.3 Delivery (per item, as each finishes, no zip)

Two messages per item, nothing else:
1. Answer key docx: `send_document(key, caption="Answer key | Lab 4 v3 [item #12]", disable_notification=True)`
2. Student docx with the buttons: `send_document(student, caption="Lab 4 (week 5): <title>\nv3 | draft | 6 rules applied\n[item #12]\nReply to this message to give feedback.", reply_markup=item_buttons(item_id), disable_notification=True)`
Buttons stay as today (Approve, Give feedback, Regenerate, Export .tex, History). Captions stay under 1024 characters.
Update `main.on_text` so reply-to feedback matches `ITEM_TAG` against `reply_to.text or reply_to.caption` (documents have captions, not text).
Update `review.send_files` accordingly; History "re-send" uses the same function with `buttons=False`.
Only the last message of a job notifies the user (below); everything else is silent.

### 10.4 Final message

For jobs with more than one item: ONE final message with sound: `Done: 8 generated, 0 failed.` and buttons `[Review items] [Retry failed]` (Retry only if failures). Also edit the status message to its final state. For single-item jobs (feedback or regenerate): no final message; the item's student message is the notification (send it WITHOUT `disable_notification`), and the status message is edited to `Lab 4 v3 ready`.

### 10.5 Message budget (testable)

A job over N items sends exactly: 1 status + 2N documents + 1 final (N>1). A feedback round sends: 1 collecting/result card (edited in place) + 1 status + 2 documents. No separate "Noted", "Queued", "Regenerating", "Feedback mode" messages.

### 10.6 Other noise fixes
- `ui.reply` splitting stays for long text but the plan display should not change.
- Callback `answer()` stays.
- Do not send `Approved ...` as a new message; edit the item's button row instead and `answer(update, "Approved")` as a toast.

---

## 11. Config, Docker, DB reset

- New env vars (document in `.env.example` and README): `OCR_ENABLED`, `OCR_LANGS`, `OCR_DPI`, `OCR_MAX_PAGES`, `EXAMPLES_PER_TYPE=2`, `EXAMPLE_MAX_CHARS=3500`, `STATUS_EDIT_MIN_INTERVAL_S=3.0`.
- Migration: `0001_initial.py` is `Base.metadata.create_all`, which does not alter existing tables, and there is no data to keep. Do not add a data migration. Add `scripts/reset_db.py` that drops all tables (including `alembic_version`) for the configured `DATABASE_URL` after a typed confirmation, and document in the README: `python scripts/reset_db.py && alembic upgrade head`. Tell me in your final report that the existing DB must be reset.
- Update `ta_course_bot_spec.md` sections that describe feedback/rules (6.5, 6.6) to match this spec, or add a short "superseded by TA_BOT_REWRITE_SPEC.md" note there.
- Update `README.md` (flow: outline -> plan -> rules -> generate -> review; new env vars; OCR).

---

## 12. Tests

Existing tests that must change (they encode the old behavior):
- `tests/test_acceptance_pipeline.py::test_feedback_loop_acceptance`, step 3 asserts that a contradicting rule auto-deactivates the old one. Rewrite: it must assert the new rule is `pending`, the old rule is still `active`, the job is not queued, and after `resolve_conflict("new")` the old is `disabled`, the new `active`, and a job exists.
- `tests/helpers.py::Scripted`: add the new system prompts (extraction with scopes, conflict check returning `reason`, format spec) with deterministic outputs. Keep it echoing active rules into output so tests can see what reached the prompt.
- `tests/test_e2e_telegram.py` and `tests/tg_harness.py`: update for the new message sequence and callback ids.

New tests:
1. `test_rules_never_auto_disabled`: add rules, run many feedback rounds including false-positive conflict output from the fake LLM; assert no rule changes status without a `resolve_conflict`/user call.
2. `test_two_feedback_rounds_keep_both`: the exact owner bug.
3. `test_scope_isolation`: item rule absent from other items' prompts; lab rule absent from tutorial prompts; course rule present everywhere.
4. `test_conflict_requires_reason`: a clash without a reason is dropped; with a reason it is shown in the card text.
5. `test_blocking_vs_informational`: same-scope clash is blocking; item-vs-course clash is a non-blocking note.
6. `test_duplicate_not_stored`.
7. `test_regen_waits_for_resolution` and `test_regen_after_last_conflict_resolved` (exactly one job).
8. `test_prompt_layers`: prompt contains TECHNICAL, DEFAULTS and RULES sections in that order; item rules last.
9. `test_heading_word_enforced` (lab with `Exercise` heading passes, `Question` fails when the type format is `Exercise`).
10. `test_format_spec_flow`: upload fixture worksheet with type, build spec, rules land as `pending/spec_review`, approve -> `active`, and they reach the lab prompt.
11. `test_examples_in_prompt`: at most `EXAMPLES_PER_TYPE`, truncated to `EXAMPLE_MAX_CHARS`.
12. `test_ocr_pdf`: build a scanned (image-only) PDF in the test by rasterizing text with Pillow and saving as PDF; assert extraction returns text and `ocr_pages`. `pytest.importorskip` / skip if `tesseract` binary is missing.
13. `test_message_budget`: with the fake Bot API harness, a 3-item job sends exactly 1 + 6 + 1 messages (count `send_message`/`send_document` calls, not edits); a feedback round on one item sends 1 + 1 + 2.
14. `test_live_message_throttle`: bursts of updates produce at most one edit per interval and never raise on `message is not modified`.
15. Callback data length: assert every `callback_data` produced in the new flows is <= 64 bytes.

---

## 13. Acceptance criteria (manual check at the end)

1. Upload an outline (also try a scanned one), confirm the plan, see the rules gate.
2. Upload 2 past lab PDFs as "Labs", build the format spec, edit one rule, approve. Generate all labs: output follows the spec and the heading word.
3. Add a course rule "use Python". Give feedback on Lab 2: "Q3 answer should be 42". Lab 2 regenerates with both. Lab 3 never shows the 42 fix.
4. Give feedback on Lab 2 again with something else. The Q3 fix is still applied. Check `/rules`: nothing disappeared.
5. Add rule "use Java" at course scope. The bot shows the conflict with a reason and four buttons, and generation is blocked until decided. Pick "Keep #old": the new rule shows under Disabled and can be restored.
6. Generate 3 items: the chat shows 1 live status message, 6 documents, 1 final message. No other bot messages.
7. Feedback round: no "Noted" replies; one card; one status; two documents.

---

## 14. Implementation order (verify each phase before the next)

1. **Schema + rules engine** (sections 3, 4): models, `services/rules.py`, unit tests with the fake LLM, update `tests/helpers.py`. No Telegram changes yet except adapting call sites so the suite still runs.
2. **Generation layering** (section 5): prompt layers, `heading_word`, examples plumbing (examples empty for now), `applicable_rules`, update `jobs.process_item`.
3. **Feedback + conflict UX** (section 6) and the **rules hub/add rule/gate** (section 7). Delete `feedback_store.py`.
4. **OCR** (section 9).
5. **Worksheets, format spec, examples** (section 8).
6. **Telegram noise** (section 10): `LiveMessage`, status message, delivery, final message, message-budget test.
7. **Docs/config** (section 11), final full `pytest`, then report: what changed, what you did not do, anything that contradicted this spec, and a reminder to reset the DB.

---

## 15. Known limits and out of scope

- Free-text format rules are followed by the LLM on a best-effort basis. Only the structural minimum (heading word, numbering, key/student question match, no disclaimers) is validated in code. Do not add a compliance-audit LLM call now; leave a clearly marked hook (`# TODO rule_audit`) after generation in `generate_item`.
- A rule that tries to override the TECHNICAL layer (for example "use \( \) for math") is not detected. Not handled in this pass.
- OCR on equations and tables is unreliable; the user is warned, nothing more.
- No change to planner, outline parser, renderer, templates, logo, auth, or the LLM router.
- No Discord version, no web UI.
