# anvil-cli

> v0.1.0 — First public release

Local LLM codes. Claude reviews. You ship.

anvil-cli wraps [aider](https://github.com/paul-gauthier/aider) so a local LLM handles the coding and Claude Code CLI reviews every change. Saves ~70% of Claude subscription tokens.

## How it works

```
You
 │
 ▼
┌─────────────────────────────────────┐
│  Aider (terminal)                   │
│                                     │
│  Chat ──► Local LLM (free)          │
│  Plan ──► Claude Code CLI           │
│  Code ──► Local LLM + Claude review │
└─────────────────────────────────────┘
```

Three phases:

1. **Chat** — brainstorm with local LLM via aider (free)
2. **Plan** — `/run anvil-plan` sends conversation to Claude for a structured task plan
3. **Build** — `/run anvil-build` feeds tasks to aider one by one; Claude reviews each edit via `--lint-cmd`

## Quick start

```bash
git clone https://github.com/steamvibe/anvil-cli
cd anvil-cli
./install.sh
```

Then in any git repo:

```bash
anvil
```

The installer asks two questions: which model to code with and which reviewer to use. Everything else is automatic.

## Requirements

- [aider](https://aider.chat) — `pip install aider-chat`
- git
- A local LLM or API model (Ollama, llama.cpp, LM Studio, vLLM, Deepseek, OpenRouter, etc.)
- Claude Code CLI — optional but recommended for review

## What happens during review

When aider edits a file, `anvil-review` runs automatically:

1. **Review 1-2:** Claude reads the diff and the actual files. If the code has real bugs (crashes, wrong logic, security issues), it says REJECTED with exact fixes. Style issues are ignored.
2. **Review 3 (escalation):** If the local LLM can't fix the issues after 2 rejections, Claude takes over and fixes the code directly using file editing tools.
3. **Timeout/rate-limit:** If Claude is unavailable, the build stops and tells you. You decide whether to continue, retry, or switch to a different reviewer.

## Troubleshooting

**"aider not found"** — Install aider: `pip install aider-chat` (or `pipx install aider-chat`)

**"No conversation history found"** — Chat with your LLM first before running `/run plan`. You need at least one exchange.

**"No plan found"** — Run `/run plan` before `/run build`. The plan lives in `.anvil/plan.md`.

**Review always approves** — Check `~/.anvil.env` — is `ANVIL_REVIEWER` set correctly? Try running `anvil-review yourfile.py` manually to see the output.

**aider crashes with thread errors** — You're inside a Claude Code session with a thread limit. Run from a plain terminal instead, or let `anvil-test-suite` auto-escape the cgroup.

**Model not responding** — Check your endpoint is running: `curl http://localhost:8080/v1/models` (or whatever your endpoint is).

## Installed scripts

All go to `~/.local/bin/`:

| Script | Purpose |
|--------|---------|
| `anvil` | Launch aider with local LLM + review config |
| `anvil-review` | Claude Code reviewer (aider lint hook) |
| `anvil-review-api` | API reviewer alternative (OpenAI-compatible) |
| `anvil-review-local` | Local LLM reviewer alternative |
| `anvil-plan` | Send aider conversation to Claude for planning |
| `anvil-plan-answers` | Follow-up for Claude's planning questions |
| `anvil-build` | Automated build loop — feeds tasks to aider |
| `anvil-test-suite` | Benchmark your coding model across 10 tasks |

## Benchmarking

Run the test suite to measure how well your coding model performs:

```bash
anvil-test-suite
# Results written to ~/anvil-test-results.md
```

Runs 10 tasks (thread-safe queue, LRU cache, retry decorator, event emitter, CSV parser, rate limiter, state machine, JSON validator, promise chain, debounced callback). Each task is reviewed by Claude. Results show pass/fail, review count, and rejection details.

**Note:** Do not run `anvil-test-suite` from inside a Claude Code session. The cgroup that Claude Code runs in has a thread limit that interferes with aider. Run it from a plain terminal instead. The script will warn you if it detects this and auto-escape, but a fresh terminal is cleaner.

## Configuration

Two config files, both written by the installer:

- `~/.aider.conf.yml` — model, endpoint, API key, lint settings
- `~/.anvil.env` — reviewer command, retry limits, diff size cap

Edit `~/.anvil.env` to switch reviewers without reinstalling:

```bash
# Claude Code CLI (subscription, recommended)
ANVIL_REVIEWER="claude -p"

# Or use an API
ANVIL_REVIEWER="anvil-review-api"
ANVIL_REVIEWER_URL="https://api.openai.com/v1"
ANVIL_REVIEWER_MODEL="gpt-4o"
ANVIL_REVIEWER_API_KEY="sk-..."

# Or use a local LLM
ANVIL_REVIEWER="anvil-review-local"
ANVIL_REVIEWER_URL="http://localhost:11434/v1"
ANVIL_REVIEWER_MODEL="auto"
```

## Model flexibility

The coding model is whatever aider supports. The installer handles config for:

- Ollama
- llama.cpp / LM Studio / vLLM (OpenAI-compatible)
- Deepseek API
- OpenRouter (any model)
- Anthropic API (Haiku/Sonnet)
- Any custom OpenAI-compatible endpoint

Override on the fly:

```bash
anvil --model openai/different-model
```

## Token economics

| Activity | Model | Cost |
|----------|-------|------|
| Chatting / brainstorming | Local LLM | Free |
| Code writing | Local LLM | Free |
| Code iteration / fixes | Local LLM | Free |
| Planning (1-3 calls) | Claude -p | ~3 subscription messages |
| Review per task (1-2 calls) | Claude -p | ~2 messages per task |
| **Typical 5-task project** | | **~15 messages vs ~50+** |

## What this is not

- Not a fork of aider — just scripts and config on top of it
- Not an Anthropic API wrapper — uses Claude Code CLI with your existing subscription
- Not a custom terminal UI — aider is the interface

## License

Apache-2.0
