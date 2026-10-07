# Lease and issue agents

Two agents for a property owner, joined at the unit:

- **Lease agent.** Reads an uploaded lease into a structured record. Every value points at the words it came from, the record is validated against `owner_ruleset.json`, and the lease is matched to a unit in the register.
- **Issue agent.** Turns photos of a unit into a condition assessment, a list of the equipment visible, and a draft work order.

Open a unit and you see its lease and the issues raised on it in one place. Nothing an agent produces takes effect until a person accepts it.

## Run it

Python 3.11 or newer.

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS and Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app
```

Open http://127.0.0.1:8000. Upload `samples/synthetic_lease.txt`, review it, link it, then report an issue on the same unit.

```bash
pytest                          # 26 tests, about a second
```

### With a real model

With no configuration the app runs on a stub model, so it works with no API key. To use Claude:

```bash
set ANTHROPIC_API_KEY=sk-ant-...        # macOS and Linux: export
uvicorn app.main:app
```

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | When set, the Anthropic adapter is used. |
| `MODEL_PROVIDER` | auto | `stub` or `anthropic`, to force one. |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Any Claude model with vision and tool use. |
| `DATA_DIR` | `var` | Where the SQLite file and uploaded photos live. Delete it to start clean. |

While no key is set, the page shows a "Demo mode" notice so nobody mistakes the stand-in's output for a real reading. The notice disappears once a real model is in use. `http://127.0.0.1:8000/#MC-B-1204` opens a unit directly.

**What I could not verify.** I had no API key, so `AnthropicModel` (40 lines in `app/model.py`) is written against the SDK documentation and has not made a live call. Everything else is exercised by the tests through the stub. If the adapter fails on first contact, that file is the only place to look.

**About the sample data.** `data/owner_ruleset.json` and `data/units.json` are the files supplied with the exercise, unchanged. `samples/synthetic_lease.txt` is a lease I wrote to exercise the rules: it fails R1, R2, R4 and R5, passes R3, R6 and R7, and states the rent two different ways. The stub's pattern matching is tuned to that file. On a different lease the stub will find less and say so through "missing" flags; a real model does not have that limit.

## How it works

```
upload ─> documents.py ─> lease_agent.py ─┐
          segments        tools + prompt  │
                                          ├─> agent.py (one loop) <─> Model: AnthropicModel | StubModel
photos ─────────────────> issue_agent.py ─┘
                          tools + prompt

agents return drafts ─> main.py stores them ─> humans accept, reject, correct ─> rules.py re-judges on every read
```

| File | Job |
|---|---|
| `app/agent.py` | The `Model` interface and the tool loop both agents share. |
| `app/model.py` | Model selection and the Anthropic adapter. |
| `app/stub.py` | The stand-in model. Same interface, no reading ability. |
| `app/lease_agent.py`, `app/issue_agent.py` | Each agent's prompt and tools. |
| `app/documents.py` | Lease to citable segments, and quote verification. |
| `app/fields.py` | The lease record's fields and type coercion. |
| `app/rules.py` | The ruleset, as deterministic code. |
| `app/db.py`, `app/main.py` | SQLite schema, HTTP API. |
| `app/static/index.html` | The whole UI. No build step. |

## Main decisions

**The model extracts, code judges.** The model is good at reading "as mutually agreed" and deciding it is not a mechanism. It is not where I want `deposit >= rent` or month arithmetic to live. So the model's job ends at facts with evidence, and `rules.py` turns facts into PASS, FAIL or NOT_DETERMINABLE. Verdicts are reproducible, they are unit tested, and they are recomputed on every read, so when a reviewer corrects the deposit, R1 changes with it. Rule results are never stored.

**A value without a findable quote is not trusted.** `record_field` takes the value, the quote and the segment id. The server then checks the quote is really in the document. If it is not, the tool tells the model so it can try again, and if it still is not, the field reaches the reviewer marked "quote not in document". This is the cheapest defence I know against a confident wrong rent.

**These are agents because the model drives.** Each agent is a loop in which the model chooses tools: it searches the unit register, records fields, checks its own work against the rules and goes back for what it missed, and raises flags. Tool errors are returned to the model instead of raised, which gives it a chance to correct a bad date or an unknown unit id. The loop is 25 lines and both agents share it.

**The stub implements the same interface as the real model.** It receives the same prompt and tools and replies with tool calls, so the loop, the tools, citation checks, validation and review all run for real with no key. It is honest about what it is: regexes for the lease, and for photos it works from the reporter's note and file names because it cannot see. I would rather show a stub that is obviously limited than one that fakes intelligence with canned answers.

**Agents propose, people dispose.** The lease agent proposes a unit match; it does not change occupancy. Occupancy changes when a person accepts the match and links the lease. Linking is refused while a high severity rule is failing unless the reviewer explicitly acknowledges it. Overrides are stored beside the agent's original value, never over it, so you can always see what the agent said and what the human changed.

**Missing is not the same as wrong.** If the agent cannot find a signature, the rule reports NOT_DETERMINABLE, not FAIL. A rejected field is treated as unknown for the same reason. A rule in the JSON with no checker in code also reports NOT_DETERMINABLE, so adding a rule to the file can never produce a silent pass.

**The unit is the join.** Leases and issues both hang off `unit_id`. The reporter chooses the unit, so the issue agent never guesses it. Before drafting, the issue agent reads the unit's active lease and open issues, which is what lets it spot a duplicate report today and decide who is liable tomorrow.

**Boring stack.** FastAPI, SQLite through the standard library, one HTML file. No ORM, no queue, no front end build. The brief is about agent design, and every extra moving part is something a reviewer has to install and read past.

## What I left out

- **Authentication and an audit trail.** There are no users, so decisions record what was decided, not who or when. This is the first thing a real deployment needs.
- **OCR.** A scanned lease with no text layer is rejected with a clear message. With Claude the fix is to send the PDF itself as a document block.
- **Background processing.** The upload request waits for the agent.
- **A generic rule language.** Rule text and severity come from the JSON; the checks are Python functions keyed by rule id. Seven rules do not justify an expression interpreter.
- **Field level confidence scores.** A model's own estimate of its confidence is not well calibrated. A verified quote is a stronger signal, so that is what the UI shows.
- **Storing the original lease file.** The segments are stored; the upload is not.
- **Editing individual observations** on an issue. The overall condition and the whole work order can be corrected.
- **Work order lifecycle** after acceptance: assignment, scheduling, completion.

## Where it breaks first at scale

1. **Synchronous agent runs.** A lease takes a model several tool round trips, and the HTTP request is held open for all of them. Twenty concurrent uploads exhaust the worker threads. Fix: a job table, a worker, and status polling. The agents are already pure functions from inputs to a draft, so they move to a worker unchanged.
2. **SQLite's single writer and local photo storage.** Fine for one owner on one machine, wrong for several instances. Fix: Postgres and object storage. The SQL is plain and lives in two files.
3. **Whole document in the prompt.** A 12 page lease fits easily. A 200 page commercial lease with schedules costs real money per run and dilutes attention. Fix: give the agent a `read_segment` and a `search_document` tool and send it an outline instead.
4. **Unit search is a linear token match.** Correct for five units, wrong for fifty thousand across owners. Fix: scope by owner and property first, then full text search.
5. **`_lease_index` scans every lease** to find each one's proposed unit. Marked in the code. Fix: store the proposed unit on the lease row.
6. **No evaluation set.** Nothing here measures extraction accuracy, so a prompt change could make things worse unnoticed. This is the one I would fix before any of the others.

## Making the product more useful

**Use the corrections.** Every accept, reject and override is a labelled example produced by the people who know the answer. Kept and replayed, they become a regression suite for prompt and model changes, and they show which fields the agent gets wrong for which landlord's template. It costs almost nothing to collect and it is what turns a demo into something you can improve with evidence.

**Sort the review by risk, not by document order.** A reviewer's attention is the scarce resource. Uncited values, fields behind a failing high severity rule, and money and dates should come first. A lease where every quote verifies and every rule passes should be approvable in one click, with the fields collapsed.

**Let the lease answer the question the issue raises.** When the AC fails, the owner's first question is "whose cost is this?" The maintenance clause is already in the record. The issue agent should cite it in the work order: "Clause 9 makes the landlord responsible for AC units." That is the real payoff of having both features around one unit, and it is one more field plus one more line of context.

**Build an asset register as a side effect.** Each issue report already lists the equipment visible. Kept per unit, that becomes an inventory with a condition history: this water heater was "good" at move in and "worn" eighteen months later. That supports deposit disputes at move out, and replacing things before they fail.

**Move in and move out comparison.** Same rooms, two sets of photos, one diff. This is where a wrong condition call costs someone money, so it is also where photo level evidence matters most.

**Dates that act.** The record holds expiry, the renewal notice period and escalation terms. Turn them into reminders: "Renewal notice for 1204 is due in 30 days", "Rent escalation applies from 1 March".

**Portfolio view of rule failures.** One lease failing R2 is a review item. Forty leases failing R2 is a template problem the owner should fix once at the source.

**Meet the reporter where they are.** A tenant reporting a leak will do it from a phone, probably through WhatsApp. The intake should ask one follow up question when the photos are unclear ("Is the water coming from the unit or the pipe behind it?") instead of producing a vague work order.

**Group duplicates.** Three tenants photographing the same lobby leak should produce one work order with three reports attached.
