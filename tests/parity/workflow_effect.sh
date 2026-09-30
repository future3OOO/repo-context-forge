#!/usr/bin/env bash
# Workflow effect N vs N+1 (issue #35 acceptance): one headless governed run of the #34
# task per arm in a bwrap estate. Arms differ only in the Repo Context Forge producer and
# the SoulForge binary; skills, model, prompt, repo and base commit are identical.
# usage: workflow_effect.sh <N-producer-repo> <N-soulforge-repo> <N+1-producer-repo> <N+1-soulforge-repo> <out-dir>
# SoulForge repos are built checkouts; each is bound over the live install directory so the
# installed `soulforge` resolves to that checkout's bin.sh, dist and node_modules together.
# Producer repos are Git checkouts whose HEAD is the producer under test.
set -euo pipefail
n_tree=$1; n_sf=$2; c_tree=$3; c_sf=$4; out=$5
estate=${RCF_EFFECT_ESTATE:-/home/prop_/.codex}
root=/home/prop_/pc-audit; prompt=${RCF_EFFECT_PROMPT:-$root/probe-intent-clean.txt}; start=f2252a4a2b60d0845c55e60d9af3cbe968ed6689
rcf_current=$(readlink -f /home/prop_/.local/share/repo-context-forge/current)
sf_install=$(dirname "$(dirname "$(readlink -f /home/prop_/.local/bin/soulforge)")")
mkdir -p "$out"
run_arm() {
  local arm=$1 tree=$2 sf=$3 est
  est=$(mktemp -d "$out/estate-$arm-XXXX")
  mkdir -p "$est/rcf-current" "$est/soulforge-home"
  # The live estate as it is now (skills, hooks, AGENTS.md, config), without its history.
  rsync -a --exclude sessions --exclude backups --exclude state --exclude worktrees --exclude tmp \
        --exclude history.jsonl --exclude session_index.jsonl --exclude 'state_*.sqlite*' --exclude 'thread_history_*.sqlite*' \
        --exclude 'memories_*.sqlite*' --exclude 'goals_*.sqlite*' --exclude 'queue_*.sqlite*' \
        --exclude 'logs_*.sqlite*' --exclude auth.json "$estate/" "$est/codex-home/"
  : > "$est/codex-home/auth.json"  # mount point; the real credentials are bound read-only below
  for part in AGENTS.md hooks.json codexs.config.toml skills hooks; do
    diff -rq --exclude __pycache__ "$estate/$part" "$est/codex-home/$part" >/dev/null \
      || { echo "ESTATE_NOT_LIVE: $part differs from $estate" >&2; exit 1; }
  done
  # The one intended estate change: Codex's default effort is medium. The lead pins low on its
  # command line and the advisor pins its own, so this reaches only unpinned calls — SoulForge's
  # `codex exec` model turns. Any other config.toml difference from live refuses the run.
  python3 - "$estate/config.toml" "$est/codex-home/config.toml" <<'PY'
import re, sys
live = open(sys.argv[1]).read()
arm, n = re.subn(r'(?m)^model_reasoning_effort = "[a-z]+"$', 'model_reasoning_effort = "medium"', live)
if n != 1:
    sys.exit("ESTATE_NOT_LIVE: config.toml has no single default model_reasoning_effort line")
open(sys.argv[2], "w").write(arm)
PY
  # SoulForge: the live config with a model this account can run.
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d["defaultModel"]="codex/gpt-5.6-sol"; json.dump(d, open(sys.argv[2],"w"), indent=2)' \
    /home/prop_/.soulforge/config.json "$est/soulforge-home/config.json"
  # Only the start commit: no origin, no later branches, so the task's real fix is not in the clone.
  git init -q "$est/repo"
  git -C "$est/repo" fetch -q --no-tags /home/prop_/projects/repo-context-forge "$start"
  git -C "$est/repo" checkout -q -B probe/$arm FETCH_HEAD
  # The producer must be a Git checkout: packets carry its commit as provenance.
  rmdir "$est/rcf-current"; git clone -q --no-hardlinks "$tree" "$est/rcf-current"
  bwrap --dev-bind / / --perms 1777 --tmpfs /tmp \
    --tmpfs /home/prop_/.gitnexus --tmpfs /home/prop_/.cache/repo-context-forge --tmpfs /home/prop_/.claude \
    --bind "$est/soulforge-home" /home/prop_/.soulforge \
    --bind "$est/codex-home" /home/prop_/.codex \
    --ro-bind /home/prop_/.codex/auth.json /home/prop_/.codex/auth.json \
    --bind "$est/rcf-current" "$rcf_current" \
    --ro-bind "$sf" "$sf_install" \
    --bind "$est" "$est" \
    -- bash -c 'printf "%s producer=%s soulforge=%s render-map=%s\n" "$1" "$(git -C /home/prop_/.local/share/repo-context-forge/current rev-parse --short HEAD)" "$(soulforge --version 2>&1 | head -1)" "$(soulforge --help 2>&1 | grep -c render-map)"' _ "$arm" > "$out/$arm.binding"
  local sandbox=(bwrap --dev-bind / / --perms 1777 --tmpfs /tmp
    --tmpfs /home/prop_/.gitnexus --tmpfs /home/prop_/.cache/repo-context-forge --tmpfs /home/prop_/.claude
    --tmpfs /home/prop_/projects --tmpfs /home/prop_/pc-audit --tmpfs /home/prop_/.cache/rcf-issue35
    --bind "$est/soulforge-home" /home/prop_/.soulforge --bind "$est/codex-home" /home/prop_/.codex
    --ro-bind /home/prop_/.codex/auth.json /home/prop_/.codex/auth.json
    --bind "$est/rcf-current" "$rcf_current" --ro-bind "$sf" "$sf_install" --bind "$est" "$est"
    -- env -u CODEX_HOME -u CODEX_THREAD_ID -u CODEX_WORKFLOW_STATE_ROOT -u OPENAI_API_KEY -u ANTHROPIC_API_KEY
       ${RCF_EFFECT_ADVISOR_MODEL:+CODEX_ADVISOR_MODEL=$RCF_EFFECT_ADVISOR_MODEL}
       ${RCF_EFFECT_ADVISOR_EFFORT:+CODEX_ADVISOR_EFFORT=$RCF_EFFECT_ADVISOR_EFFORT}
       /home/prop_/.local/bin/codex)
  local lead=(-m "${RCF_EFFECT_LEAD_MODEL:-gpt-5.6-sol}" -c "model_reasoning_effort=\"${RCF_EFFECT_LEAD_EFFORT:-low}\""
    -c "projects.\"$est/repo\".trust_level=\"trusted\"" --dangerously-bypass-approvals-and-sandbox -C "$est/repo")
  # RCF_EFFECT_TUI=1: the lead runs as a visible interactive session in this terminal (one arm per
  # terminal); otherwise headless with the JSON event stream. The model, prompt and estate are the same.
  if [ -n "${RCF_EFFECT_TUI:-}" ]; then
    "${sandbox[@]}" --no-alt-screen "${lead[@]}" "$(cat "$prompt")" || echo "$arm exited $?" >> "$out/$arm.stderr"
  else
    "${sandbox[@]}" exec "${lead[@]}" --json -o "$out/$arm.last.txt" - < "$prompt" \
      > "$out/$arm.events.jsonl" 2> "$out/$arm.stderr" || echo "$arm exited $?" >> "$out/$arm.stderr"
  fi
  cp -a "$est/codex-home/sessions" "$out/$arm-sessions" 2>/dev/null || true
  git -C "$est/repo" diff --stat > "$out/$arm.diffstat"; git -C "$est/repo" diff > "$out/$arm.diff"
  echo "$est" > "$out/$arm.estate"
}
# RCF_EFFECT_ONLY=base|cand runs one arm (one terminal per arm); the report needs both.
only=${RCF_EFFECT_ONLY:-both}
case $only in
  base) run_arm base "$n_tree" "$n_sf" ;;
  cand) run_arm cand "$c_tree" "$c_sf" ;;
  *) run_arm base "$n_tree" "$n_sf" & run_arm cand "$c_tree" "$c_sf" & wait ;;
esac
[ "$only" != both ] || python3 "$(dirname "$0")/workflow_effect_report.py" "$out"
