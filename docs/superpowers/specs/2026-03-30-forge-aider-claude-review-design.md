# Forge: Aider + Hercules + Claude Code Review

**Date:** 2026-03-30
**Status:** Draft
**Owner:** Petru

## Problem

Claude Code does everything — chatting, planning, coding, reviewing, debugging. This burns through subscription limits fast. The M5 machine has Hercules (Qwen3-Coder-Next, 204K context) and Hermes (Qwen3.5-9B) running locally for free. These models can handle the bulk of coding work. Claude should only be used where its quality matters: planning and reviewing.

## Solution

Use **aider** (open-source AI coding CLI) as the terminal interface, with **Hercules** as the coding model and **Claude Code CLI** (`claude -p`) as the planner and reviewer. Everything runs in one terminal. Claude is called via short, focused `claude -p` invocations — not interactive sessions.

## Architecture

```
User
  │
  ▼
┌─────────────────────────────────────┐
│  Aider (terminal REPL)              │
│                                     │
│  /chat ──► Hercules (localhost:8081) │  ← free, local
│  /plan ──► claude-plan.sh           │  ← subscription
│  /code ──► Hercules + lint-cmd      │  ← free + review
│            └──► claude-review.sh    │  ← subscription
└─────────────────────────────────────┘
```

## Workflow Phases

### Phase 1: Idea Chat (Hercules)

User launches aider with Hercules as the model:

```bash
forge   # alias for aider with Hercules config
```

User types their idea. Hercules asks questions, explores approaches, builds a rough plan. This is normal aider `/chat` mode — no file edits, just conversation. Zero Claude tokens.

### Phase 2: Planning (Claude Code CLI)

When the rough plan is ready, user types:

```
/run forge-plan
```

This executes `forge-plan.sh`, which:

1. Extracts the conversation so far from aider's chat history file (`.aider.chat.history.md`)
2. Calls `claude -p` with the conversation and a planning prompt:
   - "Here's a rough plan from a brainstorming session. Produce a detailed implementation plan with numbered tasks. If you have questions, list them at the top under QUESTIONS."
3. Parses Claude's response:
   - If QUESTIONS section exists → displays them in the terminal
   - User answers in aider → another `/run forge-plan-answers "answers here"`
   - Second `claude -p` call with original plan + answers → final task list
   - If no questions → shows task list directly
4. Saves the task list to `.forge/plan.md` in the project directory
5. User reviews and approves

**Claude tokens used:** 1-3 short `claude -p` calls.

### Phase 3: Coding + Review (Hercules codes, Claude reviews)

User types:

```
/run forge-build
```

This executes `forge-build.sh`, which:

1. Reads `.forge/plan.md`
2. For each task:
   a. Feeds the task description to aider as a message (aider sends it to Hercules)
   b. Hercules writes code, aider applies edits and commits
   c. `--lint-cmd claude-review.sh` fires automatically after edits
   d. `claude-review.sh` grabs the git diff, calls `claude -p "Review this diff for task N. APPROVED or REJECTED with feedback."`
   e. If APPROVED → move to next task
   f. If REJECTED → aider feeds feedback to Hercules, retries (up to 2 attempts)
   g. If still rejected after 2 retries → calls `claude -p` with full context and `--allowedTools "Read,Write,Edit,Bash"` so Claude fixes it directly
3. After all tasks complete, generates a summary report

**Claude tokens used:** ~2 calls per task (review + possible retry). For a 5-task project: ~10-15 `claude -p` calls, each short.

## Components to Build

### 1. `forge` launcher (shell alias/script)

```bash
#!/bin/bash
# /usr/local/bin/forge or ~/.local/bin/forge
OPENBLAS_NUM_THREADS=1 \
OPENAI_API_BASE=http://127.0.0.1:8081/v1 \
OPENAI_API_KEY=not-needed \
aider \
  --model openai/Qwen3-Coder-Next \
  --no-analytics \
  --no-show-model-warnings \
  --no-show-release-notes \
  --lint-cmd "forge-review" \
  --auto-lint \
  "$@"
```

Works from any directory. No config file needed.

### 2. `forge-review` (Claude review lint command)

```bash
#!/bin/bash
# Called by aider --lint-cmd after every edit
# Receives changed filenames as arguments
# Exit 0 = approved, non-zero = rejected (aider retries)

FILES="$@"
[ -z "$FILES" ] && exit 0

DIFF=$(git diff HEAD~1 -- $FILES 2>/dev/null)
[ -z "$DIFF" ] && exit 0

RESULT=$(claude -p "Review this code change. Be concise.
If correct: say APPROVED.
If issues: say REJECTED and list what to fix.

Diff:
$DIFF" --allowedTools "Read,Grep,Glob" --output-format text --max-turns 1 2>&1)

echo "$RESULT"

echo "$RESULT" | grep -q "APPROVED" && exit 0 || exit 1
```

### 3. `forge-plan` (planning script)

```bash
#!/bin/bash
# Called via: /run forge-plan
# Reads aider conversation history, sends to Claude for detailed planning

HISTORY=".aider.chat.history.md"
if [ ! -f "$HISTORY" ]; then
    echo "No conversation history found. Chat first, then plan."
    exit 1
fi

# Extract user/assistant exchanges (skip aider metadata)
CONVERSATION=$(grep -E "^(####|>|[A-Z])" "$HISTORY" | tail -100)

mkdir -p .forge

RESULT=$(claude -p "You are a senior software architect. Below is a rough plan from a brainstorming session.

Produce a detailed implementation plan with numbered tasks. Each task should have:
- A clear title
- What files to create or modify
- Acceptance criteria
- Dependencies on other tasks

If you need clarification, put questions under a QUESTIONS: header at the top.
If the plan is clear enough, skip the questions and go straight to the task list.

Conversation:
$CONVERSATION" --allowedTools "Read,Grep,Glob" --output-format text --max-turns 5 2>&1)

echo "$RESULT"

# Save plan
echo "$RESULT" > .forge/plan.md
echo ""
echo "Plan saved to .forge/plan.md"
echo "Review it, then run: /run forge-build"
```

### 4. `forge-plan-answers` (answer Claude's questions)

```bash
#!/bin/bash
# Called via: /run forge-plan-answers "answer1. answer2. answer3."
ANSWERS="$1"
PLAN=$(cat .forge/plan.md 2>/dev/null)

RESULT=$(claude -p "You previously produced this plan and asked questions:

$PLAN

The user answered:
$ANSWERS

Now produce the final implementation plan with numbered tasks. No more questions." --allowedTools "Read,Grep,Glob" --output-format text --max-turns 5 2>&1)

echo "$RESULT"
echo "$RESULT" > .forge/plan.md
echo ""
echo "Updated plan saved to .forge/plan.md"
echo "Review it, then run: /run forge-build"
```

### 5. `forge-build` (automated build loop)

```bash
#!/bin/bash
# Called via: /run forge-build
# Reads .forge/plan.md, feeds tasks to aider one by one

PLAN=".forge/plan.md"
if [ ! -f "$PLAN" ]; then
    echo "No plan found. Run forge-plan first."
    exit 1
fi

echo "Starting build from plan..."
echo "Hercules will code. Claude will review after each edit."
echo "Check .forge/build-log.md for progress."
echo ""
echo "The tasks from your plan will now be fed to aider."
echo "Aider's --lint-cmd will trigger Claude review automatically."
echo ""

# Extract task titles and descriptions from the plan
# Feed them as aider messages
cat "$PLAN"

echo ""
echo "Copy each task above into aider one at a time."
echo "Claude review triggers automatically after each edit via --lint-cmd."
```

Aider has a Python scripting API that lets us drive it programmatically:

```python
from aider.coders import Coder
from aider.models import Model
from aider.io import InputOutput

io = InputOutput(yes=True)
model = Model("openai/Qwen3-Coder-Next")
coder = Coder.create(main_model=model, io=io, auto_lint=True, lint_cmds={"python": "forge-review"})

for task in tasks:
    print(f"\n[Task {task['id']}] {task['title']}")
    coder.run(task["description"])
```

The `forge-build` script will be a Python script that:
1. Parses tasks from `.forge/plan.md`
2. Creates an aider Coder instance with Hercules + Claude lint
3. Feeds each task to `coder.run()` sequentially
4. The `--lint-cmd` fires automatically after each edit
5. Displays progress and results

Fully automated — no pasting required.

## Configuration

### Aider config file (`~/.aider.conf.yml`)

```yaml
model: openai/Qwen3-Coder-Next
openai-api-base: http://127.0.0.1:8081/v1
openai-api-key: not-needed
auto-lint: true
lint-cmd: forge-review
no-analytics: true
no-show-model-warnings: true
no-show-release-notes: true
map-tokens: 1024
```

With this config, bare `aider` from any directory automatically uses Hercules and Claude review.

### Environment variable

```bash
# ~/.bashrc
export OPENBLAS_NUM_THREADS=1
```

## Model Flexibility

The `forge` launcher defaults to Hercules, but any model works:

```bash
forge                                    # Hercules (default, free)
forge --model openai/Hermes              # Hermes (smaller, faster, free)
forge --model deepseek/deepseek-coder    # Deepseek (cheap API)
forge --model anthropic/claude-haiku     # Haiku (cheap API)
```

The reviewer is always Claude Code CLI (`claude -p`) regardless of which coding model is used.

## Token Economics

| Activity | Model | Cost |
|---|---|---|
| Chatting/brainstorming | Hercules | Free |
| File reading/repo mapping | Hercules | Free |
| Code writing (bulk of work) | Hercules | Free |
| Code iteration/fixes | Hercules | Free |
| Planning (1-3 calls) | Claude -p | ~3 subscription messages |
| Review per task (1-2 calls) | Claude -p | ~2 subscription messages per task |
| Escalation/fix (rare) | Claude -p | ~1 subscription message |

**Typical 5-task project:** ~15 Claude messages instead of ~50+ if Claude did everything. ~70% reduction.

## What We're NOT Building

- A custom terminal UI (aider already has one)
- An Anthropic-to-OpenAI translation proxy
- A fork of aider (we use it as-is with config + scripts)
- Interactive Claude sessions within aider (we use one-shot `claude -p`)

## Files to Create

### In the GitHub repo (what users clone/download)
```
install.sh                   # interactive installer
scripts/forge                # launcher script
scripts/forge-review         # review dispatcher (reads ~/.forge.env)
scripts/forge-review-api     # API-based reviewer alternative
scripts/forge-review-local   # local LLM reviewer alternative
scripts/forge-plan           # planning script
scripts/forge-plan-answers   # answer Claude's questions
scripts/forge-build          # automated build loop (Python)
templates/aider.conf.yml     # config template
templates/forge.env          # reviewer config template
README.md                    # concept, demo, setup instructions
```

### Installed on user's machine (by install.sh)
```
~/.local/bin/forge           # launcher
~/.local/bin/forge-review    # reviewer dispatcher
~/.local/bin/forge-review-api    # API reviewer (optional)
~/.local/bin/forge-review-local  # local reviewer (optional)
~/.local/bin/forge-plan      # planner
~/.local/bin/forge-plan-answers  # plan Q&A
~/.local/bin/forge-build     # build automation
~/.aider.conf.yml            # aider config (model + endpoint)
~/.forge.env                 # reviewer config
```

## Installer (`forge-setup`)

A single script users download and run after installing aider:

```bash
curl -fsSL https://raw.githubusercontent.com/<repo>/main/install.sh | bash
```

Or clone and run:

```bash
git clone https://github.com/<repo>/forge-cli
cd forge-cli
./install.sh
```

### What `install.sh` does

1. **Check dependencies**
   - `aider --version` — fail with install instructions if missing
   - `claude --version` — warn if missing, offer alternatives (API reviewer, local reviewer)
   - `git --version` — fail if missing
   - `curl` — needed for API reviewer fallback

2. **Interactive setup** (5 questions)

```
Forge Setup
───────────

Coder model (the LLM that writes code):

  1. Local LLM — Ollama (auto-detects running models)
  2. Local LLM — LM Studio / llama.cpp (OpenAI-compatible endpoint)
  3. API — Deepseek
  4. API — OpenRouter (any model)
  5. API — Anthropic (Claude Haiku/Sonnet)
  6. Custom OpenAI-compatible endpoint

Choice [1]: 2
Endpoint URL [http://localhost:8080/v1]: http://localhost:8081/v1
Model name [auto]: Qwen3-Coder-Next

Reviewer (checks code after each edit):

  1. Claude Code CLI (subscription — recommended)
  2. Claude API (pay per token)
  3. OpenAI API (GPT-4o)
  4. Local LLM (same or different model)
  5. None (skip review)

Choice [1]: 1

✓ Aider found (v0.86.2)
✓ Claude Code found (v2.1.87)
✓ Local LLM responding at http://localhost:8081/v1

Installing scripts to ~/.local/bin/...
  ✓ forge
  ✓ forge-review
  ✓ forge-plan
  ✓ forge-plan-answers
  ✓ forge-build
Writing ~/.aider.conf.yml...
  ✓ Config written

Done. Type 'forge' in any git repo to start.
```

3. **Copy scripts** to `~/.local/bin/` with correct permissions
4. **Write config** — `~/.aider.conf.yml` with chosen model/endpoint
5. **Write `~/.forge.env`** — reviewer config

### `~/.forge.env` (reviewer config)

```bash
# Reviewer command — receives filenames, exits 0 for approved
FORGE_REVIEWER="claude -p"

# For API reviewer (no Claude Code):
# FORGE_REVIEWER="forge-review-api"
# FORGE_REVIEWER_MODEL="gpt-4o"
# FORGE_REVIEWER_API_KEY="sk-..."

# For local LLM reviewer:
# FORGE_REVIEWER="forge-review-local"
# FORGE_REVIEWER_URL="http://localhost:11434/v1"
# FORGE_REVIEWER_MODEL="llama3"

# Max retries before escalation (0 = no retries)
FORGE_MAX_RETRIES=2
```

The `forge-review` script reads `~/.forge.env` and routes to the right reviewer. Users change one file to switch reviewers — no script editing needed.

### Reviewer alternatives (for users without Claude Code)

**`forge-review-api`** — calls any OpenAI-compatible API for review:
```bash
#!/bin/bash
# Uses FORGE_REVIEWER_MODEL and FORGE_REVIEWER_API_KEY from ~/.forge.env
DIFF=$(git diff HEAD~1 -- "$@" 2>/dev/null)
curl -s "$FORGE_REVIEWER_URL/chat/completions" \
  -H "Authorization: Bearer $FORGE_REVIEWER_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"$FORGE_REVIEWER_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Review this diff. APPROVED or REJECTED with feedback.\n\n$DIFF\"}]}" \
  | python3 -c "import sys,json; r=json.load(sys.stdin)['choices'][0]['message']['content']; print(r); sys.exit(0 if 'APPROVED' in r else 1)"
```

**`forge-review-local`** — same but hitting a local endpoint, no API key needed.

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Hercules produces bad code | Claude catches it in review. 2 retries, then Claude fixes directly. |
| `claude -p` calls are slow | Each call is focused and short. Max-turns capped. |
| Aider's `--lint-cmd` doesn't pass enough context | Review script grabs full git diff independently. |
| Hercules tool calling breaks | Aider handles tool translation — model just proposes edits as text. |
| Plan extraction from chat history is messy | Grep for user/assistant messages, ignore aider metadata. |

## Future Improvements (v2)

- Fully automated task feeding (Python wrapper drives aider programmatically)
- RAG injection: query M5 Hub before each task so Hercules has up-to-date docs
- Cost tracking: log Claude -p usage alongside hercules-stats
- Multiple reviewer models: run Hermes as first-pass reviewer, Claude only for what Hermes flags
