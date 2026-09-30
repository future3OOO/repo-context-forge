#!/usr/bin/env python3
"""Packet parity N vs N+1 over the recorded replay intents (issue #35 acceptance).

Usage: replay.py <N-tree> <N-binary> <N+1-tree> <N+1-binary> [--corpus DIR] [--only ID]...
For each intent, both arms bootstrap a shared clone at the recorded start commit
(intent mode, GitNexus off) and the required checks / targets are compared.
Fails (PARITY_LOST) when N+1 drops a required check or a target N had."""
import argparse, json, os, subprocess, sys, tempfile
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("n_tree"); ap.add_argument("n_bin"); ap.add_argument("c_tree"); ap.add_argument("c_bin")
ap.add_argument("--corpus", default="/home/prop_/pc-audit/replay-corpus")
ap.add_argument("--only", action="append", default=[])
ap.add_argument("--extra", action="append", default=[], metavar="ID:ROOT:START:INTENT_FILE",
                help="an additional intent outside the corpus (the #34 task)")
args = ap.parse_args()
corpus = Path(args.corpus)
records = [json.loads(line) for line in (corpus / "results.jsonl").read_text().splitlines() if line.strip()]
for extra in args.extra:
    rid, root, start, intent_file = extra.split(":", 3)
    records.append({"id": rid, "repo": Path(root).name, "root": root, "start": start, "intent_file": intent_file})
if args.only:
    records = [r for r in records if r["id"] in args.only]


def bootstrap(tree, binary, repo, cache, home, intent, out):
    env = {**os.environ, "HOME": str(home), "OPENAI_API_KEY": "x", "OPENAI_BASE_URL": "http://127.0.0.1:9"}
    proc = subprocess.run([sys.executable, str(Path(tree) / "scripts/codex_context_bootstrap.py"), "--repo", str(repo),
                           "--mode", "intent", "--intent", intent, "--cache-dir", str(cache), "--gitnexus-mode", "off",
                           "--soulforge-bin", binary, "--map-timeout-ms", "1", "--packet-json-out", str(out)],
                          env=env, capture_output=True, text=True, timeout=900)
    if not out.exists():
        return {"error": proc.stderr[-400:], "rc": proc.returncode}
    packet = json.loads(out.read_text())
    plan = packet.get("gitnexus_plan") or packet.get("gitnexus", {}).get("plan") or []
    return {
        "rc": proc.returncode,
        "required": sorted(f"{i['kind']} {i['file']} {i.get('qualified_target') or i['target']}" for i in plan if i.get("required")),
        "targets": [t["path"] for t in packet.get("targets", [])],
        "render_rank": [t.get("render_rank") for t in packet.get("targets", [])],
        "warnings": packet.get("warnings", []),
        "tokens": len(proc.stdout) // 4,
    }


lost = []
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    home = tmp / "home"; (home / ".soulforge").mkdir(parents=True)
    (home / ".soulforge" / "config.json").write_text(json.dumps({"defaultModel": "openai/gpt-4o-mini", "telemetry": False, "onboardingComplete": True, "semanticSummaries": "synthetic", "repoMapTokenBudget": 16000}))
    for rec in records:
        intent = Path(rec.get("intent_file") or corpus / "intents" / f"{rec['id']}.txt").read_text()
        root = Path(rec["root"])
        src = root if (root / ".git").exists() else None
        if src is None:
            print(f"{rec['id']} SKIP: root {root} missing"); continue
        repo = tmp / "repos" / rec["id"]
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(src), str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-B", "replay", rec["start"]], check=True)
        arms = {}
        for arm, tree, binary in (("N", args.n_tree, args.n_bin), ("N+1", args.c_tree, args.c_bin)):
            arms[arm] = bootstrap(tree, binary, repo, tmp / f"cache-{arm}-{rec['id']}", home, intent, tmp / f"{rec['id']}-{arm}.json")
        n, c = arms["N"], arms["N+1"]
        if "error" in n or "error" in c:
            print(f"{rec['id']} ERROR N={n.get('error', 'ok')[:120]!r} N+1={c.get('error', 'ok')[:120]!r}"); lost.append(rec["id"]); continue
        missing_checks = sorted(set(n["required"]) - set(c["required"]))
        missing_targets = [t for t in n["targets"] if t not in c["targets"]]
        added = [t for t in c["targets"] if t not in n["targets"]]
        reordered = n["targets"] != [t for t in c["targets"] if t in n["targets"]]
        status = "PARITY_LOST" if missing_checks or missing_targets else "ok"
        if status != "ok":
            lost.append(rec["id"])
        print(f"{rec['id']} {rec['repo']:18s} {status:11s} required N={len(n['required'])} N+1={len(c['required'])} "
              f"targets N={len(n['targets'])} N+1={len(c['targets'])} added={added} reordered={reordered} "
              f"render={'yes' if any(r is not None for r in c['render_rank']) else 'no'} tokens N={n['tokens']} N+1={c['tokens']}")
        if missing_checks or missing_targets:
            print(f"    missing checks={missing_checks} targets={missing_targets}")
        for w in c["warnings"]:
            if "render" in w.lower():
                print(f"    warning: {w}")
print("OK" if not lost else f"PARITY_LOST {lost}")
sys.exit(1 if lost else 0)
