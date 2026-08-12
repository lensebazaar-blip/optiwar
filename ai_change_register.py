#!/usr/bin/env python3
"""AI change register, derived rather than typed (Gate-1 item F).

Every meaningful change to AI behaviour should be auditable — but a register
somebody has to remember to update is wrong within a month, and it turns normal
development into paperwork. So this one is *derived* from two records that
already exist and cannot be forgotten:

  git history            what changed, when, by which commit and PR
  deployment manifests   which release put a given repo commit on the box
                         (deploy.py already writes one per release)

The judgement it adds is classification: which commits are AI-behaviour changes
at all, and of what kind — prompt, model/provider, policy, capacity,
instrumentation, closure. That is the part a reader wants and neither git nor the
manifests can express.

    python3 ai_change_register.py --since 2026-07-01
    python3 ai_change_register.py --manifests /root/deploy_releases
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess

# path -> the aspect of AI behaviour it governs. Order matters only for reading;
# a commit can touch several.
AI_PATHS = {
    "ai_client.py": "model/provider",
    "ai_model_registry.py": "model/provider",
    "chat.py": "prompt",
    "chat_gateway.py": "policy",
    "acr.py": "policy",
    "acr_closure_job.py": "closure",
    "acr_report.py": "instrumentation",
}

# Subject-line and path signals that refine the class. A commit touching
# ai_client.py may be a capacity change or a model change; the two have very
# different blast radii and should not read the same in an audit.
SUBJECT_SIGNALS = (
    (r"\bprompt|system message|instruction", "prompt"),
    (r"\bmodel\b|deepseek|openai|gpt-|provider", "model/provider"),
    (r"capacity|slots?|deadline|timeout|throttl|load.?shed", "capacity"),
    (r"canonical|event|instrument|telemetry|report", "instrumentation"),
    (r"collation|closure|sweep|outcome|attribution", "closure"),
    (r"gate|canary|handover|safeguard|safety|unsafe", "policy"),
)

NOISE = re.compile(r"^Merge (pull request|branch|remote-tracking)", re.I)


def classify(paths, subject):
    """Change classes for one commit, or an empty set when it is not an AI change.

    Paths decide *whether* it counts; the subject refines *what kind*. A commit
    that touches no AI path is not an AI change however it is worded — otherwise
    the register fills with copy edits that mention the assistant."""
    hit = {AI_PATHS[p] for p in paths if p in AI_PATHS}
    if not hit:
        return set()
    for pattern, cls in SUBJECT_SIGNALS:
        if re.search(pattern, subject or "", re.I):
            hit.add(cls)
    return hit


def git_commits(since=None, repo=None):
    """[(sha, iso_date, subject, [paths])] newest first, merge commits dropped."""
    cmd = ["git", "log", "--no-merges", "--name-only",
           "--pretty=format:%x01%H%x02%aI%x02%s"]
    if since:
        cmd.append("--since=%s" % since)
    out = subprocess.run(cmd, cwd=repo or os.path.dirname(os.path.abspath(__file__)),
                         capture_output=True, text=True, check=True).stdout
    entries = []
    for chunk in out.split("\x01"):
        if not chunk.strip():
            continue
        head, _, rest = chunk.partition("\n")
        sha, date, subject = head.split("\x02")
        paths = [p for p in rest.split("\n") if p.strip()]
        if NOISE.match(subject):
            continue
        entries.append((sha, date, subject, paths))
    return entries


MANIFEST_RE = re.compile(
    r"^release\s+(?P<release>\S+)\s*$.*?^repo main @ (?P<commit>[0-9a-f]+)\s*$",
    re.M | re.S)


def parse_manifest(text):
    """A deploy.py manifest -> {release, commit, files:{name: (old, new)}}.

    Returns None for anything that does not carry both a release id and the repo
    commit it was built from — a manifest that cannot say what it deployed is
    not evidence."""
    m = MANIFEST_RE.search(text or "")
    if not m:
        return None
    files = {}
    for line in text.splitlines():
        fm = re.match(r"^(\S+\.py) ([0-9a-f]{6,}) -> ([0-9a-f]{6,})", line)
        if fm:
            files[fm.group(1)] = (fm.group(2), fm.group(3))
    return {"release": m.group("release"), "commit": m.group("commit"),
            "files": files}


def load_manifests(directory):
    out = []
    if not directory or not os.path.isdir(directory):
        return out
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name, "manifest.txt")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            parsed = parse_manifest(fh.read())
        if parsed:
            out.append(parsed)
    return out


def deployed_in(sha, manifests, is_ancestor):
    """Earliest release whose repo commit contains ``sha``, or None.

    ``is_ancestor(a, b)`` is injected so the join is testable without a repo and
    honest when history is unavailable: an unknown ancestry answers "not
    deployed" rather than guessing."""
    for man in sorted(manifests, key=lambda m: m["release"]):
        try:
            if is_ancestor(sha, man["commit"]):
                return man["release"]
        except Exception:
            continue
    return None


def _git_is_ancestor(repo):
    def check(a, b):
        return subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                              cwd=repo, capture_output=True).returncode == 0
    return check


def build(since=None, manifests_dir=None, repo=None):
    repo = repo or os.path.dirname(os.path.abspath(__file__))
    manifests = load_manifests(manifests_dir)
    is_ancestor = _git_is_ancestor(repo)
    register = []
    for sha, date, subject, paths in git_commits(since=since, repo=repo):
        classes = classify(paths, subject)
        if not classes:
            continue
        register.append({
            "commit": sha[:9],
            "date": date[:10],
            "subject": subject,
            "classes": sorted(classes),
            "paths": sorted(p for p in paths if p in AI_PATHS),
            "release": deployed_in(sha, manifests, is_ancestor),
        })
    return register


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", help="e.g. 2026-07-01")
    ap.add_argument("--manifests", default=os.environ.get(
        "OPTIWAR_RELEASES", "/root/deploy_releases"),
        help="directory of deploy.py release manifests")
    args = ap.parse_args()

    register = build(since=args.since, manifests_dir=args.manifests)
    print("AI CHANGE REGISTER  (%d entries%s)" % (
        len(register), " since %s" % args.since if args.since else ""))
    for e in register:
        print("\n  %s  %s  %s" % (e["date"], e["commit"],
                                  ",".join(e["classes"])))
        print("    %s" % e["subject"])
        print("    paths     %s" % ", ".join(e["paths"]))
        print("    deployed  %s" % (e["release"] or "not in any release manifest"))
    if not register:
        print("  (no AI-behaviour commits in range)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
