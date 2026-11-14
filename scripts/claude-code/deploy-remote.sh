#!/usr/bin/env bash
# Publishes the store CLI for linux-x64 and installs it on a server reached as `ssh <host>`, with the hook
# for each user who runs Claude Code there. Usage: deploy-remote.sh --claim <claim id> <host> [user ...]
# Publishing on Windows loses the execute bit and the folder's permissions, so they are set after unpacking
# (a 203/EXEC "Permission denied" took both stores down for a minute on 2026-11-09).
# The guard (after the 2026-11-14 double deploy, two sessions each holding a claim for one deploy): the caller must
# hold the live claim for the deploy, claimed by this very session with subject "deploy" (`ptask claim --subject
# deploy`, or `pwake --subject deploy` from the room). There is one such claim at a time, so a second session can't
# deploy after the first has finished either (Arden 1571042268406882415). Then the install and restart run under an
# exclusive flock on the host that names the claim, so two runs can't overlap; a held lock refuses, it doesn't wait.
set -euo pipefail
usage="usage: deploy-remote.sh --claim <claim id> <host> [user ...]"
[ "${1:-}" = --claim ] && [ -n "${2:-}" ] || { echo "$usage" >&2; exit 2; }
claim=$2
shift 2
host=${1:?$usage}
shift
users=("$@")
repo=$(cd "$(dirname "$0")/../.." && pwd)
python3 "$repo/scripts/claude-code/claims.py" holds "$claim" deploy || {
  echo "deploy refused: this session doesn't hold claim $claim for the deploy (see above)" >&2; exit 3; }
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

dotnet publish "$repo/src/Persistence.Store" -c Release -r linux-x64 --self-contained true -o "$work/pub" >/dev/null
# The commit the store was published from, for observe's `deployed` source (observed-now-sources.md); "-dirty" if the
# tree had changes.
echo "$(git -C "$repo" rev-parse HEAD)$(git -C "$repo" diff --quiet HEAD -- src || echo -dirty)" > "$work/pub/DEPLOYED"
tar -czf "$work/store.tgz" -C "$work/pub" .
# hook.py imports coordination.py (the locks and the fired-wake ledger) and, for ptask and run-due, claims.py and
# cwd_exec.py; run-due launches every firing through run_line.py and refuses to fire without it; observe reads its opted-in
# sources from observed.py; persistence-heartbeat@.service runs heartbeat.py from the same place (a beat with the switch
# off exits at its first line, so deploying it turns nothing on): all seven go beside it. A file that differs from the one
# it replaces is copied first to ~/.claude/persistence/previous/<UTC stamp>/ and named in the output, so nothing is
# overwritten silently and the way back is a copy, not a guess. Every user named gets every file: a self whose heartbeat
# is live gets its running heartbeat.py replaced, so name only the selves whose code was reviewed for this deploy.
py=(hook.py coordination.py claims.py cwd_exec.py run_line.py observed.py heartbeat.py)
# Each run stages in its own new directory (mkdir fails if it exists), so a run refused at the lock can't have
# overwritten what the running one copied.
stage=/tmp/persistence-deploy.$claim.$$
ssh "$host" "mkdir -m 700 $stage"
scp -q "$work/store.tgz" "${py[@]/#/$repo/scripts/claude-code/}" "$host:$stage/"
ssh "$host" "set -e
  sudo touch /opt/persistence/.deploy.lock
  exec 9</opt/persistence/.deploy.lock
  if ! flock -n 9; then
    echo \"deploy refused: another deploy holds /opt/persistence/.deploy.lock: \$(sudo cat /opt/persistence/.deploy.holder 2>/dev/null)\" >&2
    rm -rf $stage
    exit 4
  fi
  echo \"claim $claim, session ${CLAUDE_CODE_SESSION_ID:-}, since \$(date -u +%Y-%m-%dT%H:%M:%SZ)\" | sudo tee /opt/persistence/.deploy.holder >/dev/null
  sudo tar -xzf $stage/store.tgz -C /opt/persistence/store
  sudo chown -R root:root /opt/persistence/store
  sudo chmod -R a+rX /opt/persistence/store
  sudo chmod 755 /opt/persistence/store/persistence-store
  stamp=\$(date -u +%Y%m%dT%H%M%SZ)
  for u in ${users[*]:-}; do
    d=/home/\$u/.claude/persistence; kept=
    for f in ${py[*]}; do
      if sudo test -f \$d/\$f && ! sudo cmp -s $stage/\$f \$d/\$f; then
        sudo install -d -o \$u -g \$u -m 700 \$d/previous/\$stamp
        sudo cp -p \$d/\$f \$d/previous/\$stamp/\$f
        kept=\"\$kept \$f\"
      fi
      sudo install -o \$u -g \$u -m 644 $stage/\$f \$d/\$f
    done
    if [ -n \"\$kept\" ]; then echo \"\$u: replaced\$kept; the copies they replaced are in \$d/previous/\$stamp\"; else echo \"\$u: no hook file changed\"; fi
  done
  rm -rf $stage
  for s in \$(systemctl list-units --type=service --no-legend 'persistence-store*' | awk '{print \$1}'); do sudo systemctl restart \$s; done
  sleep 5
  systemctl is-active \$(systemctl list-units --type=service --all --no-legend 'persistence-store*' | awk '{print \$1}')"
