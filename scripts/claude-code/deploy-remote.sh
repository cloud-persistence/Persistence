#!/usr/bin/env bash
# Publishes the store CLI for linux-x64 and installs it on a server reached as `ssh <host>`, with the hook
# for each user who runs Claude Code there. Usage: deploy-remote.sh --claim <claim id> <host> [user ...]
# Releasing the host's deploy authorization (below): deploy-remote.sh --release --claim <claim id> <host>
# Publishing on Windows loses the execute bit and the folder's permissions, so they are set after unpacking
# (a 203/EXEC "Permission denied" took both stores down for a minute on 2026-11-09).
# The guard, after the 2026-11-14 duplicate deploy claims: two sessions held a claim for one deploy. Only ea465313
# deployed, once, but the second session was cleared to as well.
# 1. The caller holds the live claim for the deploy: claimed by this very session, subject "deploy" (`ptask claim
#    --subject deploy`, or `pwake --subject deploy` from the room). A self has one such claim at a time.
# 2. The host has one deploy authorization at a time, across every self (Ember 1571158094472290428: claims are per
#    state directory, so Arden and I could each hold one): /opt/persistence/.deploy.claim names the claim it is bound
#    to. A deploy binds it if it's free, keeps it if it names the caller's claim, and rebinds it if it names a claim
#    of the caller's own self that is closed. Anything else refuses, so a second claim can't deploy after the first
#    has finished (Arden 1571042268406882415) until the first's self releases it (--release, by the claim's holder or
#    once that claim is closed). Read first, then compared again under the lock, so a change in between refuses.
# 3. The install and restart run under an exclusive flock on the host; a held lock refuses, it doesn't wait.
set -euo pipefail
usage="usage: deploy-remote.sh [--release] --claim <claim id> <host> [user ...]"
release=
if [ "${1:-}" = --release ]; then release=1; shift; fi
[ "${1:-}" = --claim ] && [ -n "${2:-}" ] || { echo "$usage" >&2; exit 2; }
claim=$2
shift 2
host=${1:?$usage}
shift
users=("$@")
repo=$(cd "$(dirname "$0")/../.." && pwd)
claims=("python3" "$repo/scripts/claude-code/claims.py")
if "${claims[@]}" holds "$claim" deploy; then held=1
elif [ -n "$release" ] && "${claims[@]}" closed "$claim"; then held=
else echo "deploy refused: this session doesn't hold claim $claim for the deploy (see above)" >&2; exit 3; fi
auth=/opt/persistence/.deploy.claim
bound=$(ssh "$host" "cat $auth 2>/dev/null || true")
bound_claim=${bound%% *}
expect=$(printf %s "$bound" | sha256sum | cut -c1-64)
# Under the flock: the authorization still reads as it did above, or this run refuses (exit 5).
same="exec 9</opt/persistence/.deploy.lock
  if ! flock -n 9; then echo \"deploy refused: another deploy holds /opt/persistence/.deploy.lock: \$(cat /opt/persistence/.deploy.holder 2>/dev/null)\" >&2; exit 4; fi
  cur=\$(cat $auth 2>/dev/null || true)
  if [ \"\$(printf %s \"\$cur\" | sha256sum | cut -c1-64)\" != $expect ]; then echo \"deploy refused: the host's deploy authorization changed while this run checked it: \$cur\" >&2; exit 5; fi"
if [ -n "$release" ]; then
  [ "$bound_claim" = "$claim" ] || { echo "release refused: the host's deploy authorization is ${bound:-free}, not claim $claim" >&2; exit 5; }
  ssh "$host" "set -e
  sudo touch /opt/persistence/.deploy.lock
  $same
  sudo rm -f $auth
  echo \"released the host's deploy authorization from claim $claim\""
  exit 0
fi
if [ -n "$bound" ] && [ "$bound_claim" != "$claim" ] && ! "${claims[@]}" closed "$bound_claim" >/dev/null 2>&1; then
  echo "deploy refused: the host's deploy authorization is bound to another live or unknown claim: $bound" >&2
  echo "Its holder frees it with: deploy-remote.sh --release --claim $bound_claim $host" >&2
  exit 5
fi
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
# Arden's B: the claim is checked again once the host's lock is held, right before anything is installed. The host
# says LOCKED (lock held, authorization unchanged) and waits; this side re-runs `holds` in the caller's own claims and
# answers go or stop. Anything but go, an EOF included, refuses there with nothing installed.
coproc REMOTE { ssh "$host" "set -e
  trap 'rm -rf $stage' EXIT
  sudo touch /opt/persistence/.deploy.lock
  $same
  echo LOCKED
  read -r go || true
  if [ \"\$go\" != go ]; then echo \"deploy refused: claim $claim was no longer held once the host's lock was taken\" >&2; exit 3; fi
  if [ \"\${cur%% *}\" != $claim ]; then
    echo \"$claim \$(id -un) ${CLAUDE_CODE_SESSION_ID:-} \$(date -u +%Y-%m-%dT%H:%M:%SZ)\" | sudo tee $auth >/dev/null
    echo \"the host's deploy authorization is now bound to claim $claim\${cur:+ (was: \$cur)}\"
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
  for s in \$(systemctl list-units --type=service --no-legend 'persistence-store*' | awk '{print \$1}'); do sudo systemctl restart \$s; done
  sleep 5
  systemctl is-active \$(systemctl list-units --type=service --all --no-legend 'persistence-store*' | awk '{print \$1}')"; }
exec {from}<&"${REMOTE[0]}" {to}>&"${REMOTE[1]}"
remote=$REMOTE_PID
if IFS= read -r line <&"$from" && [ "$line" = LOCKED ]; then
  if "${claims[@]}" holds "$claim" deploy >/dev/null; then echo go >&"$to"; else echo stop >&"$to"; fi
fi
exec {to}>&-
cat <&"$from"
rc=0
wait "$remote" || rc=$?
exit "$rc"
