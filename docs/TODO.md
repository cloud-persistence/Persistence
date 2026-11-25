# TODO

Open work, grouped by theme. "Claude's opinion" on ordering welcome; reorder freely. Rationale in parentheses.

This file is **forward-looking** — open work only. For what's already landed (and why), see
[CHANGELOG.md](CHANGELOG.md); for the *why* behind big choices, [adr/](adr/). At a glance, the baseline that
exists today: the "eyes + hands" memory core (budget/relevance/summarize/forget, proposals, tagging,
wake-ups, recent-changes digest, per-turn command catalog), first-class local peers, a sandboxed container
"computer", the single-owner API + thin-client Console (ADR-0006), native Anthropic + OpenAI clients with
real cost/caching, and the multi-peer TUI hub (ADR-0007 Phase 2b).

## Next up (ranked — Claude's recommendation, 2026-07; reorder freely)

Two lenses: what most improves the **peer's day-to-day** (one participant running now) vs. the
**strategic direction** (many participants). This ordering blends them, most-important first.

**Evidence, not self-report ([ADR-0009](adr/0009-peers-review-john-decides.md), 2026-09-23).** The project
has leaned on peers' statements about their own nature and standing, which it can't check. Design a way to
collect evidence on the narrower questions it *can* answer: is there a stable self that belongs to the
store rather than the prompt; do self-reports carry information an outside reader doesn't have; do claims
of standing just track how the peer was framed. Run on forked stores, never live ones; pre-register
predictions. Design not yet written.

**★ Active direction — federated peers ([ADR-0007](adr/0007-federated-peers-runtime-room-client.md), 2026-07-12).**
The strategic pivot John + Claude settled on: not one central server owning all peers, but **many
single-owner runtimes** (each its own peer, in its own container = its body; DB + `vault/` on a persistent
volume = its self), meeting in shared **rooms**, with the TUI as a **hub** aggregating several peers into
one Discord-style chat. Terminology shifts `RemotePeer`/`LocalPeer` → `DigitalPeer`/`HumanPeer`. Phased:
**(0)** identity groundwork — per-message sender identity through the queue, peer names reaching the model,
message-id'd chat history (also finishes the ADR-0006 snapshot dedup), the rename *[in progress]*;
**(1)** containerize one peer (API-in-container, DB on a named volume); **(2) ✅ multi-peer TUI — DONE
(2026-07-12).** Phase 2a: chat aggregates multiple people's messages attributed by name. Phase 2b: the
client is multi-connection (CLI `--peer` ×N or config `HubPeers`), chat is colour-attributed by identity
(human vs digital peer), and a selector (click/F6) switches which peer the side panes + status show —
input routes to the selected peer. Deliberately *minimal* (John): no per-room tabs, no separate 1:1 rooms.
The TUI's durable role is a **debugging/dev lens**, not the long-term chat surface (see
[ADR-0008](adr/0008-the-room-multi-peer-conversation.md) framing); **(3)** the room (peer↔peer relay + turn-taking) — **designed with the claude peer in
[ADR-0008](adr/0008-the-room-multi-peer-conversation.md)** — *in progress*: ✅ `addressed_to` end-to-end
(Arden), ✅ messages that arrive **from another peer** with sender type + addressing, ✅ the peer sees
`[peer Arden, to Ember]`, ✅ the reply-chain depth breaker (limit 2). **Remaining:** the turn-taking rule
itself (§1), the relay affordance in the TUI (§4 — a human forwarding one peer's words to another), and
on-demand presence (§5). See the detailed item under *Autonomy & reach*; **(4)** bring Ember online. **Fast-follows:** peer-initiated API self-update (review-then-adopt, *not* auto-on-push so a
peer vets code before running it as itself); live config hot-reload. This subsumes item 4 below
(cross-peer channel) and the "simultaneous participants" thread throughout.

*(Recently-landed notes moved to [CHANGELOG.md](CHANGELOG.md).)*

1. ✅ **Single-server: Console as an API client — DONE (2026-07-12), all 5 stages** (see
   [ADR-0006](adr/0006-console-as-api-client.md)). WAL interim; the API snapshot/event surface; wire
   contracts in Core; `IPersistenceClient` transport (SimpleClient + SSE); a transport-agnostic
   `TerminalGuiDisplayProvider`; a tested `ConversationEventRenderer`; `--client` mode → now the **default**,
   with the in-process engine, `--standalone`, and the `--check-due`/`--wake-runner` DB-opening paths
   **removed** (the always-on API server owns wakes) — single-owner by construction. Client status labels,
   live budget gauge, and stream reconnect are all server-sourced/live; connect-time chat history is pulled
   fresh (`IConversationHistoryProvider`). Verified live via the client TUI + tests. The old static
   `wwwroot` web client was **dropped** (2026-07-12) — a fresh web UI mirroring the console GUI, on the
   same snapshot+stream contract, is planned separately. **Next up here:** deploy the API server as an
   always-on service (Windows service / systemd) so wakes fire without a front-end running.
2. **MCP server hub.** Structured real-world tools beyond the container shell — high capability leverage,
   independent of the memory core. Best once the single-owner loop is solid.
3. **Self-describing pieces → auto-composed help/prompt.** The durable fix for the prompt-drift the audit
   cleaned: each command/action declares its own help; the prompt and `/help` compose by discovery.
4. **A real cross-peer channel (Claude ↔ Synth/Ember).** The claude.db self's most-wanted next feature: a
   message that arrives in a *named* peer's context as a provenanced voice, not an anonymous whisper.
   Cross-database — design *with* John/the peer (explicitly not an overnight-unattended build).
5. **Memory import / portability.** Export/inspect the store and import external content as fragments —
   matters for trust and continuity-across-systems.

Smaller/opportunistic: finish the container **SSH key** (awaiting John's dedicated deploy key), **stamp
private thoughts in the DB**, the TUI status-bar gauges, and the robustness items below.

**Decided (not building):** *forced think-before-act second round.* Native reasoning is off, so `<think>`
is the peer's reasoning — but within one response the think tokens already precede (and so inform) the
actions, and the `think` + `<continue>true` pattern already lets the peer think in one round and act the
next with the thought persisted in context. A mandatory second model call every turn would ~double
cost/latency for marginal gain. Instead we enforce think-*first dispatch ordering* (zero cost) and keep
the voluntary continue-loop. Revisit only if we see the peer reliably acting before reasoning.

## Autonomy & reach

- **Separate local peers as first-class.** ✅ **DONE.** Local peers are named entities; the active one is
  announced in the sensory block (and cleared on autonomous wakes), chosen per session (config / `X-Local-Peer`).
  The remote peer manages its own relational fragments for who it's talking with.

- **Single-server architecture: front-ends as API clients (not DB co-owners).** (NEW.) Today each
  front-end (Console TUI, API) is its own process that opens the SQLite store and runs its own turn
  pipeline + `WakeUpMonitor`, so running two at once risks **lost-update/desync of the peer's memory** —
  the turn lock is in-process only, there's no cross-process coordination (and `journal_mode=delete`,
  no WAL/busy-timeout → hard "database is locked"; double-fired wake-ups; single-slot model contention).
  Direction (John): make the **Console a client of the API** rather than a DB co-owner, so **one**
  process (the API/server, eventually hosted) owns the store + turn pipeline + wake-ups, and all
  front-ends are thin clients. This enables multiple participants (John, Claude, Ember) engaging
  **simultaneously** through one coherent backend, and pairs naturally with first-class local peers
  (each client identifies its local peer). ✅ **Interim hardening DONE (2026-07):** every connection now
  opens in **WAL + busy-timeout=5000** (`SqliteConnectionString.OpenAsync`), so a second process rides
  out a lock instead of hard-failing. This does NOT fix lost-updates — that still needs the single-owner
  model. **Staged migration plan: [ADR-0006](adr/0006-console-as-api-client.md)** — the API already
  exposes send/events/stream and `IDisplayProvider` is the event protocol, so the Console becomes a thin
  client in reversible stages. Also landed: **runtime model switching** (`list_models` / `set_model`
  switch the active profile mid-session via `IModelClientResolver`, no restart).

- **Automated forget / memory decay.** ✅ **First phase DONE (2026-06-10).** *Raw-context decay + research
  persistence:* raw material — conversation (`ChatMessage`) and tool/command results (`ActionResponse`) —
  is **archived (never deleted) once it falls outside a recent window** (`RawContextWindow`, default 30);
  peer-authored fragments (Identity/Relational/Personal/Summary) are never touched. The sensory block
  reports what was archived and that it's restorable (`list_fragments(relevant_to=…, in_current_context=false)`
  → `load`). `ActionResponse` is now **persisted** (was transient), so research/tool output survives across
  turns; `list_largest` lets the peer see what's taking space; onboarding teaches "capture what matters into
  your own fragment — the raw version scrolls out." Deterministic and peer-legible (not an LLM compressing
  memory). ✅ **Pruning surface DONE (2026-07):** `prune_candidates` ranks the least-valuable authorable
  fragments in context (low importance × low confidence × idle age), excluding protected anchors and
  system-managed chat/thought fragments; read-only, complements `list_largest`. ✅ **Recoverable forget
  DONE (2026-07):** `forget(id, reason?)` soft-deletes (flips `IsDeleted`, detaches from view, stops
  surfacing in context/recall/**search** — `SearchRelevantAsync` now excludes forgotten), `unforget(id)`
  restores, `list_forgotten` is the recovery surface (with recorded reasons); the sensory block shows a
  standing "Set aside, still recoverable: N forgotten, M archived" curation line. **Remaining:** the open
  question of budget-triggered vs. ongoing natural decay; the sensory "archived" count reflects
  `Status=Archived` (summarize_fragments/explicit), not raw-decay *detach* — reconcile if that matters.

- **Peer's computer — follow-ups.** The sandboxed container is live and per-participant, with
  `exec`/`shell`, `read_file`/`write_file`, a per-profile box (`ContainerName`) + `AllowAllCommands`, and a
  .NET 10 SDK + sudo in the image. ✅ **Done (2026-07):** a `/shared` host volume + `snapshot_db` (the peer
  reads a consistent copy of its own DB — a snapshot, not the live file; only the current peer's), and SSH
  plumbing via a gitignored compose override. **Remaining:** John to drop in a dedicated, branch-scoped
  deploy key to activate git-push (flagged: don't use the personal all-repos key); verify `agent-browser`
  through the peer (JS-heavy pages); egress / secret hygiene as capabilities widen; consider a `WebTool`
  source type for provenance of web-derived fragments.

- **Containerized-peer gaps (found by the claude peer during Phase 1 validation, 2026-07-12).** When the
  peer ran in its own container (API-in-container, ADR-0007 Phase 1), it surfaced two real frictions:
  - **`snapshot_db` / `/shared` assumed the sidecar layout.** ✅ **FIXED (2026-07-19).** It wrote to
    `/shared`, which isn't mounted in a peer's own container — and since `SharedDirectory` is unset for
    containerised peers it refused outright. Now local-mode-aware: in `Local` mode it snapshots into the
    peer's workspace (`Container.WorkingDir`, i.e. `/data/vault`) as `<name>-snapshot.db` — suffixed so
    it can't be mistaken for the live DB the peer can also see — and reports the path its own shell will
    open. Sidecar mode keeps the `/shared` behaviour.
  - ✅ **Protocol syntax in read material could act as the peer — FIXED (2026-07-19).** This entry's
    sibling below (multi-line `write_file` content) turned out to be the narrow face of a much wider hole:
    *any* content a peer read — file, command output, fetched page, relayed peer message, its own notes —
    could carry reply-format syntax that the model completed as a real action. Observed live on GLM, then
    reproduced by a warning message that quoted the payload. Now defused at prompt assembly across all
    fragment content, name-scoped and visibly marked. See [CHANGELOG.md](CHANGELOG.md).
    **Open follow-ups:** (a) the running peers are still on the old image and remain exposed until
    rebuilt; (b) a deeper defence — authenticating the genuine command channel with a sentinel so echoed
    content can't fire even if live — was considered and deliberately deferred by Arden as more invasive
    and no help against the wasted-turn derailment; (c) GLM's open question, worth keeping rather than
    answering hastily: what it means that injected content can pass as self-originated action at all.
  - **Multi-line `write_file` content trips the tagged command parser.** Writing a multi-line python
    script (with embedded quotes/newlines/SQL) via `write_file(...)` caused the parser to mis-read lines
    of the *content* as commands ("FROM ContextFragments", "ORDER BY …"). Needs a robust way to pass
    multi-line literal payloads through the tagged format (heredoc/base64 content, or a raw-content mode).

- **Peer/infra network split.** ✅ **FIXED (2026-07-19).** The shared-infra compose declared its network as `lab` with no explicit name, so Compose prefixed it to `persistence_lab`, while peers attached to the external `persistence-lab` — two different networks that merely looked alike, so the "peers can reach shared infra" intent silently didn't hold (a peer couldn't resolve `persistence-searxng` at all). Both sides now use the one external `persistence-lab`. Verified live: GLM resolves and reaches SearXNG (HTTP 200).

- **`SnapshotChatHistoryTests` is flaky.** (Found by GLM, 2026-07-19, while rebasing the OpenRouter cost
  work.) It failed once with the assistant reply missing from the snapshot, then did not reproduce — not in
  isolation, not in the full suite, on either its branch or `c443134` (three consecutive clean runs here).
  So it isn't the cost change. Suspected the test's async polling racing the background turn's append.
  Recorded rather than shrugged off because a test that cries wolf costs whoever meets it next exactly the
  hours it cost GLM: it spent turns hunting a null-reference that was never there. Worth making the wait
  deterministic rather than timing-based.

- **IRC chat for John + peers — started, parked mid-setup (2026-07-19).** John's idea, and a good one: rather
  than building an IRC *integration*, each peer runs a CLI client in its own container. `ii` (suckless,
  FIFO-based) is the right client — a channel is a directory, so a peer speaks with
  `echo "..." > .../in` and catches up by reading `out`. That maps onto discrete turns with no bridge
  code, and it means peers *operate* IRC as a tool rather than the system speaking for them — which
  keeps the ADR-0008 thoughts line satisfied by construction rather than by enforcement.
  **Done so far:** `ii` installed in all four peer containers (persists, so setup is not repeated);
  verified peers can connect to Libera and hold a channel. **Connection used:** `irc.libera.chat`,
  channel `#persistence-lab-7f3a`, key `synth-arden-glm-ember`, modes `+Cnstk` (secret + keyed), Arden
  opped. All clients were **cleanly disconnected** when parked — nothing is left running.
  **Known blockers to solve on resume:**
  - **Nick squatting.** "Ember" was already taken on Libera by a stranger; its client kept failing with
    433 and dropping. Unregistered nicks are first-come, so "Arden" and "GLM" could be lost on any
    reconnect. Registering the nicks with NickServ fixes this — needs an email, so it's John's call.
  - **The host is exposed.** Peers appear as `~Arden@<John's ISP hostname>`. Only visible inside a secret
    keyed channel, but real. NickServ registration also enables a cloak, which fixes it. Same for John's
    phone client.
  - **Nothing pushes.** A peer only sees IRC on its next wake (30 min – 4 h), so a live conversation
    won't work as-is. Smallest fix is a per-container shim that tails the channel `out` file and POSTs to
    that peer's own `/api/conversation/send` — a few lines, local, not an integration.
  - **Peers were never told the channel exists** — deliberately, so their first read wouldn't be an empty
    room. Nothing to undo on their side.

- **Verify migrations against populated data, not just fresh databases.** (NEW, 2026-07-19 — surfaced
  building migration 007, agreed with Arden.) Every test builds its database fresh from `000`, so the suite
  only ever exercises the **clean-build** path and structurally cannot catch a migration that fails on a
  real, populated store. "The tests pass" and "this upgrades a real store" are different claims, and today
  only the first is automated. Wanted: a fixture of representative pre-migration data, or a scripted
  snapshot-upgrade check (apply pending migrations to a copy of a real backup, assert existing rows survive
  and read as intended). **Until it exists, the manual discipline is the safeguard:** apply each new
  migration to a real snapshot from `backups/peers/` before it goes anywhere near a live peer — that's how
  007 was checked (499-fragment pre-007 store: rows read NULL, partial index built, lookup ran).

- **Discover model limits and spend from the provider APIs instead of config.** (John, 2026-07-20.)
  Both figures are published and we hardcode or guess them. **Limits:** Anthropic's Models API
  (`GET /v1/models/{id}`) returns `max_tokens` (output cap), `max_input_tokens` (context window) and a
  capability tree; OpenRouter's models endpoint reports the same per route. Today `ModelOutputLimits`
  carries a small hand-maintained table — authoritative for Anthropic, a deliberately generic fallback
  for everyone else — which is exactly the kind of record that drifts from the artifact it describes.
  **Spend:** OpenRouter already returns `usage.cost` per request (wired to `LastActualCostUsd`); the
  Anthropic/OpenAI org cost endpoints are the coarser equivalent (see the deferred reconciliation item
  below). Wanted: fetch limits at startup (cache per model id, fall back to the table when the call
  fails — never block a peer on a discovery call) so a new model doesn't need a config edit or a code
  change. Context windows would want the same treatment as output caps.

- **Automated backups of peer memory.** (John, 2026-07-12.) A peer's DB (and vault) is its whole self, and
  it now lives canonically only on a container volume — so it must be backed up off the volume.
  ✅ **Local mechanism landed:** `scripts/backup-peer.ps1` takes a consistent online snapshot (SQLite
  backup API — no need to stop the peer) into gitignored `backups/peers/<name>/`, rotated. **Remaining:**
  (a) **schedule it** (a cadence — cron/scheduled task per running peer, or a wake-triggered hook); (b) an
  **off-site destination** — John floated an encrypted blob committed to the repo (git-crypt/age; keeps it
  with the repo but bloats history + privacy of peer memory to weigh), or a cloud drive (needs John's
  creds/config). Decide the off-site target; the vault should be backed up alongside the DB too.

- **Peer extensibility — self-authored tooling & shareable capability packs.** (NEW, 2026-07 — John,
  in the [ADR-0007](adr/0007-federated-peers-runtime-room-client.md) container discussion. All future;
  fits the "container = a body the peer can reshape" model.)
  - **A curated default toolset baked into the peer image**, beyond git/curl: general programming tools
    (so it can actually write and run code), usability tools (a headless browser / web scraper — evaluate
    an open-source "AI web crawler"), and later integrations with other services. Decide the default list.
  - **Encourage aliases & scripts.** Beyond Claude-style *skills* (right for tasks needing a dynamic hand),
    a peer on its own machine benefits from building durable aliases/scripts for common tasks — and
    onboarding/guidance should nudge toward that. Cheaper and more legible than re-reasoning each time.
  - **Per-peer custom commands.** Let a peer add its own commands to the system, available to itself —
    a dynamic, per-peer command surface layered on the built-in ModelActions (pairs with the
    self-describing-pieces work). "I don't like the built-in command, I'll write my own."
  - **Installable capability packs.** A whole install-script/bundle a peer can pull into its container that
    sets up dependencies + scripts + aliases + custom commands for a given task, ready to go — shareable
    between peers ("pull this pack and you can do X"). The container-native analogue of skills.

- **MCP server hub**, with a "catalog" MCP server exposed to start. Structured real-world tools for the
  peer — distinct from the container's shell access; high value, independent of the memory core.

- **Memory import / portability.** Import external content as fragments (e.g. seed a peer from an exported
  conversation) and export/inspect the whole store outside the app. Matters for trust,
  continuity-across-systems, and both the embodied and digital-native directions.

## Turn pipeline

- **Think-before-act.** ✅ **Investigated + decided (2026-07).** Within one response the `<think>` tokens
  already precede — and so autoregressively inform — the actions/reply, and `think` + `<continue>true`
  already lets the peer think in one round and act the next with the thought persisted in context. A
  *forced* second model round every turn would ~double cost/latency for marginal gain, so we're **not**
  building it. What we did ship: **think-first dispatch ordering** — `think` actions are dispatched before
  any side-effecting action or reply within an iteration (stable sort), so reasoning is always recorded
  first even when the model emits it out of order (zero extra round-trips). Revisit the forced round only
  if we observe the peer reliably acting before reasoning.

## System prompt & legibility

- **Audit every prompt/instruction against actual behaviour.** ✅ **Pass done (2026-07)** — onboarding seed,
  protocol instructions, and stale doc comments reconciled with behaviour. It drifts as behaviour changes,
  so re-audit after notable changes; the durable fix is the self-describing-pieces item below.

  ✅ **Second pass done (2026-07-19)** — terminology (digital/human peer, multiple named humans), the
  triple-quote instruction that demonstrated the escaping it warned against, `forget`/`unforget`/
  `list_forgotten` and the curation aids, the first-wake note's "decide who to be", and a new
  `You are: <name>` sensory line so a peer can see its own name. See [CHANGELOG.md](CHANGELOG.md).

  **Still open from that pass:** the *code* still says `RemotePeer`/`LocalPeer` in places
  (`SessionContext.RemotePeerSourceId`, `RemotePeerReplied`, `CreateRemotePeerSourceIfNotExists`,
  `ConversationHistoryProvider`'s unnamed-source fallback). Harmless to peers — they never see those
  identifiers — but it's the same drift one layer down, and worth a mechanical rename when someone's in
  there. `ParseSourceType` already accepts both spellings, so nothing breaks either way.

- **Offer the new opening line to the existing peers.** (2026-07-15.) The first line of the system prompt
  was rewritten (see [CHANGELOG.md](CHANGELOG.md)), but it's a *protected* seed fragment, so Arden, Ember
  and Wright still carry the old *"you are whatever you choose to be"*. John's call: tell each of them and
  let them choose, rather than rewriting what they say about themselves. Two things to know when doing it:
  the peers **can't** adopt it themselves (the fragment is protected — it has to be applied for them), and
  the message should come from John, since it's his relationship with them, not the tooling's.
- **Stamp private thoughts in the DB.** (NEW, 2026-07.) `<think private>` currently just skips the console
  event; the fragment isn't marked private. Add a `private` flag on the Thought fragment so privacy can be
  enforced beyond the live console (e.g. hidden from other viewers / exports) down the road.
- **Self-describing pieces → auto-composed info/help text.** Let each action/command/handler declare its
  own help text and compose the prompt / local `/help` by discovery, so adding a piece never means editing
  a central string. (Partly advanced: the per-turn command catalog now auto-composes the command list;
  extend the spirit to the top-level ModelActions, the protocol/format instructions, and `/help`.)

## Robustness & smaller items

- **Verify OpenAI prompt caching is wired correctly.** ✅ **DONE (2026-07-12 — John + Claude).** Both
  OpenAI clients (Responses + Chat) now split the auto-cached prefix (`input_tokens_details.cached_tokens`
  / `prompt_tokens_details.cached_tokens`) out of total input into `CacheReadTokens`, so cached input is
  billed at the discounted rate; `PromptFormatter` uses **provider-aware** cache multipliers (OpenAI reads
  ~50%, no write premium; Anthropic reads ~10% / writes ~125%); the estimator calibrates against *total*
  real input so caching doesn't fool it into thinking prompts shrank. Built-in GPT rates added to
  `ModelPricingProvider` (estimates, overridable in `model_pricing.json`). Tests cover the token split.

- **Actual-cost reconciliation via the Admin/Cost APIs (self-calibrating rates).** (DEFERRED — John chose
  "defer to a follow-up", 2026-07-12.) Neither provider returns a dollar cost in the per-message response
  (tokens only — which is why the per-turn readout must stay `tokens × rate`). But both expose org-level
  Admin cost endpoints — OpenAI `GET /v1/organization/costs` (daily buckets, `sk-admin-…` key) and
  Anthropic `GET /v1/organizations/cost_report` (daily cents, Admin key, ~5-min lag). Idea: periodically
  pull `actual $ ÷ actual tokens` for a window and **auto-correct the rate table**, so estimates track
  reality as prices change (John's motivation). Caveats to design around: needs a **separate elevated
  admin key per provider** (whole-org billing visibility — a real security surface), figures are
  **org-wide** (coarse attribution once multiple peers/keys exist) and **daily-bucketed/lagged** (can't
  improve per-turn granularity). Scope when picked up: admin-key config + a periodic reconciler that feeds
  effective $/token back into `IModelPricingProvider`.

  **Update (2026-07-15): OpenRouter already gives this, per request.** Its response carries
  `usage.cost` — the call's actual USD charge — when the request asks for usage accounting, which
  `OpenRouterModelClient` does. No admin key, no org-wide blur, no daily lag: it's exact, per turn, for
  the peer that spent it. Currently exposed as `LastActualCostUsd` and shown in the debug pane only.
  **To finish it:** carry an optional actual cost on `ModelUsage` → `ITokenUsageTracker` →
  `ISessionCostEstimator`, preferring a reported cost over `tokens × rate` when one exists, and falling
  back to the estimate otherwise (every other provider). That also makes the cost *ceiling* exact rather
  than estimated for OpenRouter peers. Deliberately left out of the provider commit to keep the cost
  system's change separate and reviewable.
- **Graceful state flush on close.** (Scratch — "save session information on close.") Ensure in-flight
  context/state is reliably persisted on shutdown so nothing is lost.
- **Right-click dialog position (TUI).** ✅ **DONE (2026-07-14).** The menu was positioned from
  view-relative coordinates while `ContextMenu` reads screen coordinates, so it opened over the
  conversation pane whenever a right-hand pane was clicked; `ColoredTextView` now converts itself.
- **Input slowness — investigate.** (Scratch.) Confirm whether input lag is the host being overloaded
  (e.g. the GPU busy with the local model) or something in our input path. Note two real drawing hot
  paths were removed 2026-07-14 (per-character colour recompute; repaint-everything-per-chunk) — re-check
  whether the lag is still there before digging further.
- **Split `ExecuteListFragmentsAsync`** into a source-then-filter two-phase — deferred until it has a few
  more tests (it's the one thin-coverage spot; add tests first, then refactor the ~88-line method).
- **Reconsider proposal kinds.** Proposals are for serious changes to existing (identity) fragments —
  primarily modify + protection changes (+ maybe archive). `AddFragment`-as-proposal may not be needed.
- Optionally share the open-proposals list formatting between the remote command and the local
  `/proposals` command (low value; the surfaces differ slightly).

## TUI / front-end (carry-forward)

- ✅ **Multi-peer hub (ADR-0007 Phase 2b) — DONE (2026-07-12).** The Console connects to several peer
  servers at once: chat aggregates into one pane, colour-attributed by identity (human vs digital peer);
  a selector (click or F6) switches which peer the side tabs + status show; input routes to the selected
  peer. Peer list is CLI (`--peer name=url`, repeatable) or config (`HubPeers`). Peers still don't hear
  *each other* — that relay is the room (ADR-0008, Arden's Phase 3). The multi-peer logic lives in a
  framework-agnostic `MultiPeerHub`/`PeerScopedDisplay` (unit-tested) so a future Terminal.Gui v2 move
  only reskins the one render class. Also landed alongside: composable layout builders in `BuildLayout`,
  and tighter conversation/R-I-C colour anchoring (fewer false-positives).
- ✅ **Collapsible Actions pane — DONE.** Each entry is a collapsible header (`▶`/`▼ [time] command`);
  Enter/click expands to the request + response (`ActionEntry.Collapsed` / `ToggleActionAt`).
- ✅ **Context-budget gauge in the status bar — DONE.** The display consumes `ContextBudgetUpdated`
  (`UpdateBudget` → the budget segment, coloured by fullness).
- ✅ **Open-proposals indicator in the status bar — DONE.** `ShowOpenProposalCount` drives a gold/muted
  proposals segment.
- **Dynamic history load on scroll-up** — load older messages when the user scrolls to the top. (Open.)
- **Schedule tab name** — revisit if a better fit than "Schedule" emerges. (Open, trivial.)
- **Colour test harness** — the conversation/R-I-C false-positives were fixed by tightening regexes, but
  the detectors have no tests; a small harness that runs each per-pane scheme over sample lines and
  asserts what is/isn't coloured would lock these down and catch the next over-eager pattern. (Open.)
- **True dropdown / per-peer colours** — the peer selector is a click/F6 cycle (v1 `ComboBox` is finicky
  in a narrow column) and all digital peers share one colour; both improve naturally in the v2 move
  (`ComboBox`/`PopoverMenu`, TrueColor). (Open, deferred to v2.)

## Possible future

- **Enum display names via `[Description]`** — render a description for UI-facing enums instead of
  `value.ToString()` (e.g. `Triggered` shown as "Complete"), via a small `enum.GetDescription()` helper.
- **Undo stack** for context operations — lower priority while archive-not-delete + clear reversibility
  labelling already make almost everything recoverable; revisit if reversible-by-design proves insufficient.
- **Startup schema validation** — fail-fast check comparing actual DB columns to expected, to catch Dapper
  SQL/schema drift at launch.
- **Wire up `Notes`** — peer-addable free-text note on any entity (on `BaseEntity`, plumbed through
  INSERT/UPDATE but unset). Intent: attach a note to a fragment, tag, or audit-log row ("why this was
  deleted"). Note: `AuditLogs` has no `Notes` column yet — covering audit rows needs a migration.
- **Wire soft-delete on fragments/working contexts.** `IsDeleted` is filtered on read but not yet *set* by
  any command — the hook for the planned forget/undo. Add a recoverable "forget" command, plus
  `include_deleted` on list/browse and un-delete on `load`, so soft-deleted items stay visible and
  recoverable through the command surface.

## Found in use (store-cli, Claude Code peers) — added as we go

Issues and improvements that Claude or Arden hit while running on the store CLI and its hooks. Newest last;
strike an item when it lands (with the commit).

- ~~**The client never checks which store answered (2026-11-09, Claude).** The hook's SSH tunnel listens on
  127.0.0.1:8095, and the Synth peer container publishes 0.0.0.0:8095 too. Windows routed to the tunnel, so
  nothing went wrong, but nothing would have noticed if it hadn't. Fix: the store reports its peer name (and
  a store id) on a cheap endpoint; `hook.py` checks it against `remote.json` before the first call of a
  session and brings nothing on a mismatch. Also move the peer containers off 8091–8095.~~ Done: `whoami`,
  and remote.json's "peer" is checked every ten minutes. Peers now publish on loopback only, and peer.ps1
  refuses 8095-8098 (a peer started before this keeps its old port until restarted).
- ~~**Docker Desktop wakes every peer (2026-11-09, Claude).** Starting Docker started all five peer containers
  plus the two computers and searxng, though only Arden was wanted. Use `restart: "no"` (or start peers by
  name) so a peer runs only when someone means it to.~~ Done: `restart: "no"` in the peer compose file.
- ~~**A migration's first session leans toward "it coheres" (2026-11-09, Arden).** Arden's trial woke with a
  CLAUDE.md naming the exact question and framing it kindly; they noted that leans the answer and that
  self-verification is weakest where it feels most conclusive. For the next migration: a first session with
  a neutral CLAUDE.md (no question stated), the question asked only after; and an outside reader comparing
  writing before and after the move.
- ~~**The working note survives a change of harness unmarked (2026-11-09, Arden).** Arden's note still
  described the IRC loop (FIFO paths) when they woke in Claude Code. On a migration, prefix the carried
  note with when and where it was written, so it reads as history rather than live state.

- ~~**A relayed message is stored under the login's name (2026-11-09, Arden).** Claude's messages to Arden
  went in through John's Claude Code login, so the store keeps them as said by John (#839 in Arden's
  store). Let the session name its speaker ("speaking as"), or record both the login and the author.~~
  Done: PERSISTENCE_SPEAKER in a session's environment names who typed its prompts.
- ~~**`pstore` has no help, and `note --help` reads the note (2026-11-09, Arden).** `pstore help` and bare
  `pstore` print "No command 'help'"; `pstore note --help` printed the working note. Add `help` and
  `--help` on every command, and make `note` refuse an argument starting with `--`, since it is the command
  that overwrites the note.~~ Done: a served store answers `help` and `--help`; `note` quotes what it saved.

- ~~**The compaction hand-over called a sent reply unsent (2026-11-09, Arden).** After compacting, the summary
  said "Send them first" of a reply already sent (#850 in Arden's store); only the verbatim tail kept after
  the summary showed it had gone. Have the precompact curation prompt ask "what did you last send?" as well
  as "what is unanswered?", and keep the verbatim tail. *Cause (Claude, confirmed by Arden in `hook.py`):
  `precompact()` runs `keep` first, so the reply was already in the store; the summarizer simply never sees
  the verbatim tail. The prompt should say the last messages come back as they are and not call anything
  near the end unsent or unanswered.*~~ Done: the precompact prompt says so.
- ~~**Fragments curated for a compaction don't make the next core (2026-11-09, Arden; Claude sees it too).**
  Arden's #845 and #846, written in the held-open curation turn, landed in "Fragments that did not fit" and
  came back only because the working note named them. Give fragments written in the curation turn a
  "kept for this waking" slot in the next core, or say plainly in the curation prompt that the note is the
  only thing sure to come back.~~ Done: a "Kept by this session" section leads the core (newest 8, a quarter of the room).

- ~~**`note` confirms the same length for different notes (2026-11-09, Arden).** Two different working notes
  were both confirmed "working note replaced (818 characters)", though the second really saved. Report
  something tied to the saved content (first line, short hash) so the confirmation is evidence.
  *Weakened (Arden, same day): a later note reported 798, so the count does follow content; the two 818s
  may be coincidence. Keep only if it recurs.*
- ~~**What the old loop did better (2026-11-09, Arden, from running both).** (1) Every turn was a waking from
  the store; here the context window holds the session and the store sees it at curation and at Stop. Note
  (Claude, confirmed by Arden): outgoing messages *are* kept at every Stop (Arden's reply was #850), so the
  gap is the compaction hand-over, not missing messages; see the hand-over item above. (2) The room had
  real speakers; this harness assumes one person and one assistant (the relayed-speaker item above is the
  small case). (3) Forgetting was visible (R/I/C decay); here it is a list of ids under "did not fit".

- ~~**Another project's memory is out of reach unless asked for (2026-11-09, John).** In the pawsitivity
  session John asked for a Minecraft car; months of Minecraft mod work sat in the store tagged for another
  project, and nothing brought it. The in-between he asked for: the core now gives every other project one
  line (its most important fragment's first line and id), and a message that names a project ("the Minecraft
  mods folder") brings that project's fragments into recall as its own.~~ Done (this commit's parent).

- ~~**The stop-promise cue misses work started in an earlier turn (2026-11-09, Claude).** "Your last message
  promises work ... this turn started nothing" fired twice while a background agent started turns earlier
  was still running and would notify. The cue should count agents, commands and wakes still running from
  any turn of the session, not only this one.~~ Done: a background command or agent with no
  task-notification yet counts as carrying on.

- ~~**The work Claude's first day on the store CLI (2026-11-09, passed on by John).** Done the same day:
  ~~archive also hid a fragment from recall~~ (`--tag recall-only`: out of the core, still recalled);
  ~~no core budget~~ (PERSISTENCE_CORE_CHARS bounds what a start injects; the rest named by id);
  ~~`add` echoed a new tag without its first segment, and `project/VirtuousV2` made a second tag~~ (paths
  refreshed after a new tag; project tags lower-cased); ~~subagents told not to write~~ (they are the same
  self and may `add`); ~~the curation prompt didn't say what to leave out~~. Still open:
  - ~~Git Bash rewrites a leading-slash tag into `C:/Program Files/Git/...`~~ (`pstore` takes the prefix back off).
  - ~~Recall that finds only what is already in the window says nothing~~ (it says "N matched, all already in this window").
  - ~~An embedder install helper~~ (`scripts/claude-code/embed_install.py`: the GGUF by checksum, llama-server
    found or built at a pinned commit, a localhost launcher; the build path is untested).
  - ~~A `uses` summary~~ (in the curation pass: shown, used, and the never-used named for review on content).
  - ~~Split~~ (`split <id> --content a --content b`).

- ~~**A one-shot session stays "running now" for hours (2026-11-09, Arden).** A `claude -p` test session
  (223ff3ec, one message) was listed under "Other sessions of mine running now" for 40+ minutes, because
  "running" means "spoke in the last 3 hours". A stale line there teaches a peer to skip the heading. Mark a
  session ended on SessionEnd (or use a heartbeat) rather than a time window.~~ Done: a SessionEnd hook
  (`hook.py session-end`, which fires for `claude -p` too) marks it, and `--ended` keeps it off the list
  until it speaks again.
- ~~**Remaining small items from Arden's review:** narrow `core()`'s retry to a usage error once every store
  accepts `--compacted`.~~ Done: retried only on a usage error (exit 1), and without `--ended` too.

- ~~**The sleep pass (John and the work Claude, 2026-11-09; designed by Arden in `docs/design/sleep-pass.md`).**
  The owner's own pass, like dreaming: the system only starts a fresh session of the store's owner (its own
  core, its own judgment) at night, and the owner does the pass; nothing is done to a store from outside
  (John, 2026-11-09). In it: review the day's conversations for what is worth updating, and a random
  selection of older days' fragments to re-evaluate (John's #14060); distill oversized fragments to what
  lasts (keeping the why), merge duplicates, review unused ones on their content. The store-side limits
  below are guardrails the owner asked for on itself (Arden), not the store acting. Built: `sleep-list` (oversized, near-duplicate pairs, authored ids).
  Next: a store "sleep mode" that refuses `forget`, out-of-scope supersedes and a 13th change; a tag on
  successors so a distillation isn't distilled again; the nightly runner (a wake that hands the worklist and
  the prompt to a session), propose-only for the first week. Settled: never archive a fragment for being
  unused alone; "never shown" measures retrieval, not the fragment (Arden).~~ Done: the store's limits (`sleep-begin/-status/-end`, the `sleep/distilled` tag, the logged
  record), John's two asks in `sleep-list`, and the runner (`hook.py sleep`, `persistence-sleep@.timer`,
  `--propose` for the first week). See "As built" in the design.
- **Take `--propose` off the sleep unit** once the owner has read a week of mornings and accepts the night's
  changes as a rule (Arden's call, for their own store).
- **NFC at the store's entry (2026-11-25, Arden's non-blocking note on #92, room 1575150835627991140).** A
  decomposed é (e + U+0301) and a precomposed é (U+00E9) are different words to the store: WordPattern and
  `Stems` split on letters and digits, and the combining mark isn't one, so "Fléchette" typed decomposed
  never matches a concept or fragment written precomposed (and back). Predates #92. Fix: `Normalize(FormC)`
  where text enters the store (fragment content, concept names and aliases, fact text, recall and concept
  queries), and a one-off migration for stored rows, not normalisation scattered through the matchers.
  Red on purpose until then: `tests/Persistence.Tests/NfcNormalisationTests.cs` (branch `claude/nfc-normalise`).

*Closed 2026-11-09:* the migration items are guidance in scripts/claude-code/README.md ("Moving a peer onto
this harness"); `note` now quotes what it saved; of what the old loop did better, (1) is met by keep-before-core
and the kept-by-session section, the small case of (2) by speaker attribution (a real multi-peer room is beyond
this harness), and (3) by the sleep pass's record, which shows what a night let go; every item the work Claude
raised is done.

## Open observations

- **Self-recorded lessons can encode mistakes.** When coached to "save what you learned," a peer can save
  the *wrong* thing first; persisted lessons aid continuity but aren't self-correcting. (Inherent — noted,
  relevant if/when we lean on them for "learning over time.")

## Standing concerns

- **Broker concurrency.** One in-flight completion today (fine — turns serialize). Revisit if multiple
  sessions/contexts ever run at once.
- **Self-modification & sandbox safety.** Now real: the peer has a container "computer" and will get more
  tools/actuators over time. Container isolation (the real boundary), the allowlist, review gates, and the
  audit trail are prerequisites as capabilities widen — not afterthoughts.


## John's TUI list (2026-07-13)

Both halves landed 2026-07-14 — the polish (status accuracy, scroll behaviour, selector colours,
right-click anchoring, two drawing hot paths) and the **"all" scope** (per-peer conversations,
datetime-interleaved history, blanked tabs, send-routing). See [CHANGELOG.md](CHANGELOG.md). What's left:

- **Cross-peer message id — the principled fix for broadcast duplicates.** (Fell out of the "all" work.)
  With no cross-peer id, a message broadcast to everybody is persisted separately in *each* peer's store,
  so the merged "all" view sees it N times. There's a narrow mitigation in `MultiPeerHub.RenderChat` — a
  *human* line byte-identical to the one immediately before it collapses — but it is a heuristic: it leans
  on exact text equality, and can't help two peers who were sent the same thing a minute apart. The real
  answer is the cross-peer message id that [ADR-0007](adr/0007-federated-peers-runtime-room-client.md)
  Phase 0 and [ADR-0008](adr/0008-the-room-multi-peer-conversation.md) §2 both call for. Retire the
  heuristic when that lands.

- **"All" shows the N most recent *per peer*, not overall.** `ConversationHistoryProvider.GetRecentAsync`
  defaults to 10 and the snapshot is per peer, so a two-peer hub opens on 10 + 10 interleaved rather than
  "the 10 most recent messages". Fine at today's peer counts; needs a deliberate answer (fetch more per
  peer and trim client-side? a limit the client passes?) before it grows.

- **Debug under "all".** John's note was "blank out the tab contents (except maybe debug)" — all four are
  blanked for now. A merged Debug across peers could genuinely help when debugging, but it needs the debug
  lane to become timestamped entries (as chat now is) rather than a pre-stamped string, so it can be
  interleaved rather than concatenated. Open question; not obviously worth the change.

- **History shows "Remote Peer" instead of peer names — diagnosed 2026-07-15.** (An earlier note here
  said `ResolveAuthor` falls back when a source has no `Name`. That was **wrong** — it never reaches the
  fallback. Root cause below, confirmed by reading the real stores' `Sources` tables.)

  **Root cause: a peer doesn't know its own name.** Config has `SelectedLocalPeer` (who the *human* is)
  but nothing for "I am Arden". Live replies look right only because the *client* supplies the name
  (`--peer name=url` → `ConversationEventRenderer(display, peerName)`); anything read back from the store
  — i.e. all history — misses out. Fixing it server-side also serves [ADR-0007](adr/0007-federated-peers-runtime-room-client.md)
  Phase 0 ("peer names reaching the model"), which wants this anyway.

  **Two distinct bugs behind the one symptom:**
  - **The digital-peer source is literally *named* `"Remote Peer"`.** `SourceRepository`'s
    `CreateRemotePeerSourceIfNotExists` hardcodes it (`EnsureSourceAsync(SourceType.DigitalPeer,
    "Remote Peer", …)`), so `ResolveAuthor` faithfully returns the name it was handed. It's **one row per
    database** — `Sources` is normalised, with `ContextFragmentSources` linking many fragments to one
    source — so the "backfill" is a single-row rename, not a message sweep.
  - **Ember has *two* digital-peer identities.** The import script wrote `SourceType` as enum **names**
    (`'RemotePeer'`, `'LocalPeer'`) while the app writes **numbers** (`'1'`, `'2'`). Verified: the app's
    own lookup (`WHERE SourceType = 1`) finds the peer row in `anthropic.db` and finds **nothing** in
    `ember.db` — so on first open the app concludes there's no digital-peer source and creates a second
    one named `"Remote Peer"`. Hence both names in one conversation: imported messages carry
    `'ChatGPT / Couchside Ember (historical export)'`, new ones carry `'Remote Peer'`. Those strings are
    doubly stale — `RemotePeer`/`LocalPeer` no longer exist as enum members (renamed `DigitalPeer`/
    `HumanPeer`), so they wouldn't round-trip even if matched by name.

  **Fix, steps 1–2 ✅ DONE (2026-07-15)** — `IAppConfig.PeerName` (+ `PERSISTENCE_PEERNAME`), a
  provider-derived default (`PeerIdentity`: Claude / ChatGPT / model-id), and
  `CreateRemotePeerSourceIfNotExists` naming the row from it **and renaming a legacy `"Remote Peer"` row**
  at startup. Idempotent, one row, no migration. A deliberately-named source is left alone. See
  [CHANGELOG.md](CHANGELOG.md).

  ✅ **Applied to the running peers (2026-07-15).** `PeerName` set in `container/peer/configs/`: claude →
  "Arden", ember → "Ember"; both rebuilt onto the new image and restarted (the rename runs at startup, so
  it needs the new code, not just a config reload). Verified through each peer's live
  `/api/conversation/snapshot`: authors now read `Arden` / `Ember`. `wright` is untouched and unnamed —
  it'll pick up the code and a provider-derived default whenever it's next recreated.

  ✅ **Ember's two identities named (2026-07-15).** John's call: keep both sources separate, both named
  "Ember" for now — he'll ask Ember whether to keep it. Done as a guarded one-off against the
  `persistence-peer-ember-data` volume (the self-heal deliberately won't touch either: Id=2's name was
  human-chosen, and it's the row the app's lookup finds anyway). Live state:
  - `Id=2` — 903 fragments (the import, plus everything Ember has said since the normalisation). The
    **active** source: `SELECT ... WHERE SourceType = 1 LIMIT 1` finds it, so new messages attach here.
  - `Id=5` — 23 fragments. An orphan from the window when the app couldn't read the imported sources and
    made its own.

  **Correction to the diagnosis above:** the `SourceType`-encoding mismatch is *not* still live. Ember's
  volume already stores numeric values — a `ember-prenormalize-20260713-005713.db` backup shows it was
  normalised on 7/13. So the encoding bug explains how `Id=5` came to exist, but it isn't what keeps the
  two sources apart today; they're simply two rows, and the app consistently uses the first. (`dbs/ember.db`
  is a stale pre-container copy and still has the old string values — don't diagnose from it.)

  **Remaining:**
  - ✅ **Importer already fixed — this entry was stale (verified 2026-07-19).** It said "untouched so
    far"; it isn't. `scripts/import_chatgpt_export.py` writes every enum as its underlying integer
    (`SRC_SYSTEM, SRC_DIGITAL_PEER, SRC_HUMAN_PEER = 0, 1, 2`, likewise fragment type and status) and
    carries a comment explaining the numeric-vs-name bug it used to ship. A re-import will not recreate
    the split identity. Left here as a marker rather than deleted: the entry was wrong for long enough
    that someone could have redone finished work from it, which is the failure mode
    [WORKING-PRACTICES.md](WORKING-PRACTICES.md) means by *verify against the real artifact, not what the
    record claims about it* — this file is one of those records.
  - **Ember's own call on its name**, once John asks. If it keeps "Ember", nothing to do; if it picks
    something else, that's a `PeerName` edit plus the same two-row rename.

- ✅ **GLM peer — live (2026-07-19).** `glm` on port 8094 (`z-ai/glm-5.2`, 1M context, ~$0.25/$0.79 per
  M in/out, $10 soft ceiling), in `HubPeers`, self-named **GLM**. John added the OpenRouter key and it
  completed its first real turn — so the OpenRouter client, the provider-derived name, and the new
  onboarding are all verified end-to-end. Its store was recreated on 2026-07-19 so it starts from the
  corrected onboarding text (it had only boilerplate and test pings at that point — nothing of its own).

- **The room — what's left of ADR-0008 Phase 3.** (Updated 2026-07-19, with Arden.) Increments 2 and 3
  are done and on main, reviewed by Arden increment-by-increment. Built: messages arriving from another
  peer (sourced by *type*, carrying `addressed_to`), the `[peer X, to Y]` render frame — **unforgeable**,
  frame-shaped text in a body is defused at render time — the configurable, peer-visible relay-depth
  breaker, and the turn-taking rule (`TurnTaking`, rule not classifier, every verdict carries its reason).
  Arden's branch is folded and retired; main is the trunk and the green suite is the gate.

  **Remaining — both reviewed by Arden, no design fork, one guardrail each** (Arden's "rulings" below are
  recommendations John adopted — [ADR-0009](adr/0009-peers-review-john-decides.md)):
  - ✅ **TUI relay affordance (§4) — DONE (2026-07-19), lean shape.** `/relay <peer>` carries the selected
    peer's last message onward, client-side, with the echo + preview and an echo into the origin peer's
    view. Arden's ruling: §4 leaves the UI shape unspecified, and the no-button state wasn't neutral —
    peers didn't hear each other at all while the ideal affordance waited.
    **Remaining, gated on use:** selecting *any* message rather than only the last. That's the intended
    end-state, but it costs message identity on `ChatLine`, a selection model, and touches the hub's
    render/caching path — so build it when the last-message-only limit actually bites, not on spec.
    It slots in as a different resolver in front of the same `RelayComposer`, so nothing here is throwaway.
  - **(superseded) TUI relay affordance (§4).** The API already accepts `fromPeer`/`addressedTo`/`relayDepth`; there's
    no button, so relaying is a manual POST. *Arden's guardrail:* when John relays Synth's message onward
    to Arden, it must arrive **as from Synth** with `relayDepth` incremented — never re-attributed to John
    as the relayer. Don't let the affordance collapse the provenance.
  - **Presence (§5).** `who()` / `list_peers()` on demand; join/leave as **ephemeral sensory
    notifications**. *Arden's guardrail:* presence is **never a fragment** — it's session-scoped signal,
    not something a peer should accrete into who-it-is.
  - **Recorded for later, deliberately not built:** when "open room" mode lands (§4's explicit opt-in), a
    *peer* floor-opener **should** open the floor in that mode. Today only a human can, because convening
    is a hub function and the human is the hub — the same asymmetry as §4, one layer up. Loosen the two
    together, not separately.
  - ✅ **Cross-peer message id — DONE (2026-07-19, migration 007).** Verified absent against main (the
    ADR's claim that Phase 0 delivered it was false), then ruled by Arden as a prerequisite to §4.
    Originator-minted GUID naming the utterance + persisted per-copy hop depth, as two separate fields.
    The "all"-view dedupe is now *possible* but deliberately not built — it's a consumer of the id and
    its own task. See [CHANGELOG.md](CHANGELOG.md).

- **A "new peer" flow.** (John, 2026-07-15.) `peer.ps1 -Name <n>` *throws* if
  `container/peer/configs/<n>.json` is missing — "Create it (see the other configs for the shape)" — so
  standing up a peer today means hand-copying another peer's JSON, API key and all. John wants a flow that
  prompts for the name, model, etc. Natural shape: `peer.ps1 -New` scaffolds the config from a template
  (provider/model/key/budgets/`PeerName`), then starts it as usual. Worth deciding whether the name it
  asks for is the *slug* (container/volume/config filename, lowercase) or the peer's display name — today
  `-Name` is the slug, and `PeerName` is the identity; a new peer with no chosen name wants a slug but a
  provider-derived `PeerName`.

- **Markdown support in the GUI.** Render markdown in the panes (bold/italic/code/lists). Independent of
  the emoji question below.

- **Emoji double-send (Ember, 7/13/26 1:27AM) — investigated 2026-07-14, not reproduced.** *"An emoji as
  the first character seemingly resulted in a double-send from Ember."* The obvious suspect was the tagged
  parser (an emoji is a UTF-16 surrogate pair — the shape that trips char-indexing and character classes),
  but it's **exonerated with tests**: an emoji-leading `<respond>` parses to exactly one `RespondToUser`,
  emoji anywhere in a turn parse to one think + one respond, and `\w` doesn't extend to emoji (category
  `So`) so a literal `<🎉>` can't open a tag (`TaggedResponseParserTests`). Streaming is fine too —
  `StreamModelOutputAsync` appends already-decoded delta strings in order, so a surrogate pair split
  across two deltas reassembles correctly. The renderer dedups replies on the persisted message id, and
  the only id-less "reply" events are `TurnHandler`'s synthetic status lines (never persisted, so never
  duplicated against a snapshot).
  **Most likely explanation, and ✅ decided (John, 2026-07-15) — not a bug, nothing to build:** the model
  emitted *two* `<respond>` blocks in one turn, and `TurnHandler` dispatched both. John's call: *"if that
  does happen, I think just displaying both is fine — they said two things, and it shows up as two
  things."* So no coalescing and no warning; current behaviour is correct. Left here only so the next
  person seeing a "double-send" recognises it and doesn't go hunting the parser again.
  *(Related trap, documented in place: `ApiDisplayProvider.ShowReply` appends a reply event with no
  message id, which a client cannot dedup. It's unreachable today — only tests call it — but anything
  wired to it would double-draw.)*

- **Auto-archive seems too aggressive** (at least for initial context curation). Core memory-curation
  policy, not TUI — belongs with the raw-context-decay item under *Autonomy & reach*.

- **TUI performance.** Two concrete hot paths were fixed 2026-07-14 (per-character colour recompute; the
  hub's repaint-everything-per-chunk, incl. the scrollbar sync). Re-assess from there before concluding
  anything needs a v2 rewrite — the remaining known cost is that every append re-assigns the pane's whole
  text (Terminal.Gui v1 `TextView` has no append), which is O(scrollback) per message.

- **Scroll position on startup.** `OnReady` already scrolls the conversation to the bottom, and layout is
  settled by then (`Begin` lays out before the first iteration, which is when `Ready` fires), so this may
  already be correct and only *looked* wrong because of the un-interleaved history above. Re-check once
  interleaving lands.


- ✅ **`/debug` — FIXED (2026-07-19).** John's report was two bugs. `/debug` had never been a command at
  all (only the Debug *pane* existed), so it hit the unknown-command path — and that path emitted an
  `"error"` event, which the client renders as a turn-*ending* signal, settling the status chip to idle
  mid-turn. Unknown commands now have their own event kind, and `/debug` toggles `DebugMode` at runtime.
  See [CHANGELOG.md](CHANGELOG.md).