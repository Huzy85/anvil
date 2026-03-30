#!/usr/bin/env bash
# Anvil installer — sets up scripts and config
# Usage: ./install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
AIDER_CONF="${HOME}/.aider.conf.yml"
ANVIL_ENV="${HOME}/.anvil.env"

echo ""
echo "  Anvil Setup"
echo "  ─────────────────────────────────"
echo "  Local LLM codes. Claude reviews."
echo ""

# ── Check dependencies ──────────────────────────────
MISSING=0

if command -v aider &>/dev/null; then
    AIDER_VER=$(aider --version 2>&1 | head -1)
    echo "  ✓ aider ($AIDER_VER)"
else
    echo "  ✗ aider not found"
    echo "    Install: pip install aider-chat"
    MISSING=1
fi

if command -v git &>/dev/null; then
    echo "  ✓ git"
else
    echo "  ✗ git not found"
    MISSING=1
fi

HAS_CLAUDE=0
if command -v claude &>/dev/null; then
    CLAUDE_VER=$(claude --version 2>&1 | head -1)
    echo "  ✓ Claude Code ($CLAUDE_VER)"
    HAS_CLAUDE=1
else
    echo "  ⚠ Claude Code not found (optional — can use API reviewer instead)"
fi

echo ""

[ "$MISSING" -eq 1 ] && echo "Install missing dependencies first." && exit 1

# ── Coder model ─────────────────────────────────────
echo "Coder model (the LLM that writes code):"
echo ""
echo "  1. Local LLM — Ollama"
echo "  2. Local LLM — llama.cpp / LM Studio / vLLM (OpenAI-compatible)"
echo "  3. API — Deepseek"
echo "  4. API — OpenRouter"
echo "  5. API — Anthropic (Haiku/Sonnet)"
echo "  6. Custom endpoint"
echo ""
read -rp "Choice [2]: " CODER_CHOICE
CODER_CHOICE="${CODER_CHOICE:-2}"

case "$CODER_CHOICE" in
    1)
        read -rp "Ollama model name [codellama]: " MODEL_NAME
        MODEL_NAME="${MODEL_NAME:-codellama}"
        API_BASE="http://localhost:11434"
        API_KEY="not-needed"
        MODEL="ollama_chat/${MODEL_NAME}"
        ;;
    2)
        read -rp "Endpoint URL [http://localhost:8080/v1]: " API_BASE
        API_BASE="${API_BASE:-http://localhost:8080/v1}"
        read -rp "Model name [auto]: " MODEL_NAME
        MODEL_NAME="${MODEL_NAME:-auto}"
        API_KEY="not-needed"
        MODEL="openai/${MODEL_NAME}"
        ;;
    3)
        read -rp "Deepseek API key: " API_KEY
        API_BASE="https://api.deepseek.com/v1"
        MODEL="deepseek/deepseek-coder"
        ;;
    4)
        read -rp "OpenRouter API key: " API_KEY
        read -rp "Model [meta-llama/llama-3-70b]: " MODEL_NAME
        MODEL_NAME="${MODEL_NAME:-meta-llama/llama-3-70b}"
        API_BASE="https://openrouter.ai/api/v1"
        MODEL="openrouter/${MODEL_NAME}"
        ;;
    5)
        read -rp "Anthropic API key: " API_KEY
        read -rp "Model [claude-haiku-4-5]: " MODEL_NAME
        MODEL_NAME="${MODEL_NAME:-claude-haiku-4-5}"
        API_BASE=""
        MODEL="anthropic/${MODEL_NAME}"
        ;;
    6)
        read -rp "Endpoint URL: " API_BASE
        read -rp "Model name: " MODEL_NAME
        read -rp "API key (empty if none): " API_KEY
        API_KEY="${API_KEY:-not-needed}"
        MODEL="openai/${MODEL_NAME}"
        ;;
    *)
        echo "Invalid choice"; exit 1 ;;
esac

# ── Reviewer ────────────────────────────────────────
echo ""
echo "Reviewer (checks code after each edit):"
echo ""
if [ "$HAS_CLAUDE" -eq 1 ]; then
    echo "  1. Claude Code CLI (subscription — recommended)"
else
    echo "  1. Claude Code CLI (not installed — install first)"
fi
echo "  2. OpenAI-compatible API"
echo "  3. Local LLM"
echo "  4. None (skip review)"
echo ""
read -rp "Choice [1]: " REVIEWER_CHOICE
REVIEWER_CHOICE="${REVIEWER_CHOICE:-1}"

REVIEWER_CMD="claude -p"
REVIEWER_EXTRA=""

case "$REVIEWER_CHOICE" in
    1) REVIEWER_CMD="claude -p" ;;
    2)
        read -rp "Reviewer API URL [https://api.openai.com/v1]: " R_URL
        R_URL="${R_URL:-https://api.openai.com/v1}"
        read -rp "Reviewer model [gpt-4o]: " R_MODEL
        R_MODEL="${R_MODEL:-gpt-4o}"
        read -rp "Reviewer API key: " R_KEY
        REVIEWER_CMD="anvil-review-api"
        REVIEWER_EXTRA="ANVIL_REVIEWER_URL=\"${R_URL}\"
ANVIL_REVIEWER_MODEL=\"${R_MODEL}\"
ANVIL_REVIEWER_API_KEY=\"${R_KEY}\""
        ;;
    3)
        read -rp "Reviewer LLM URL [http://localhost:11434/v1]: " R_URL
        R_URL="${R_URL:-http://localhost:11434/v1}"
        read -rp "Reviewer model [auto]: " R_MODEL
        R_MODEL="${R_MODEL:-auto}"
        REVIEWER_CMD="anvil-review-local"
        REVIEWER_EXTRA="ANVIL_REVIEWER_URL=\"${R_URL}\"
ANVIL_REVIEWER_MODEL=\"${R_MODEL}\""
        ;;
    4) REVIEWER_CMD="true" ;;
    *) echo "Invalid choice"; exit 1 ;;
esac

# ── Install scripts ─────────────────────────────────
echo ""
echo "Installing scripts to ${BIN_DIR}/..."
mkdir -p "$BIN_DIR"

for script in anvil anvil-review anvil-review-api anvil-review-local anvil-plan anvil-plan-answers anvil-build anvil-help; do
    if [ -f "${SCRIPT_DIR}/scripts/${script}" ]; then
        cp "${SCRIPT_DIR}/scripts/${script}" "${BIN_DIR}/${script}"
        chmod +x "${BIN_DIR}/${script}"
        echo "  ✓ ${script}"
    fi
done

# ── Write configs ───────────────────────────────────
echo ""

# Aider config
sed -e "s|__MODEL__|${MODEL}|" \
    -e "s|__API_BASE__|${API_BASE}|" \
    -e "s|__API_KEY__|${API_KEY}|" \
    "${SCRIPT_DIR}/templates/aider.conf.yml" > "$AIDER_CONF"
echo "  ✓ ${AIDER_CONF}"

# Anvil env
sed "s|__REVIEWER__|${REVIEWER_CMD}|" \
    "${SCRIPT_DIR}/templates/anvil.env" > "$ANVIL_ENV"

if [ -n "$REVIEWER_EXTRA" ]; then
    echo "" >> "$ANVIL_ENV"
    echo "$REVIEWER_EXTRA" >> "$ANVIL_ENV"
fi
echo "  ✓ ${ANVIL_ENV}"

# ── Verify PATH ─────────────────────────────────────
if [[ ":$PATH:" != *":${BIN_DIR}:"* ]]; then
    echo ""
    echo "  ⚠ ${BIN_DIR} is not in your PATH."
    echo "  Add to your shell profile:"
    echo "    export PATH=\"\${HOME}/.local/bin:\${PATH}\""
fi

echo ""
echo "  ─────────────────────────────────"
echo "  Done. Type 'anvil' in any git repo to start."
echo ""
