using Persistence.Config;
using Persistence.Data.Entities;
using Persistence.Store;
using System.Text.Json;

const string Usage = """
    persistence-store: a peer's Persistence store, worked from an agent harness (e.g. Claude Code).

    The store is a file of its own: --db <path>, or PERSISTENCE_STORE_DB. Each peer keeps its own.
    The peer's name (whose voice the authored fragments are) is --peer, PERSISTENCE_PEERNAME, or Claude.

      add --type <Identity|Relational|Personal|Summary> --content <text|-> [--content-file <path>]
          [--summary <text>] [--importance 0-1] [--confidence 0-1] [--tag <a/b>]... [--protected]
          [--project <name> | --everywhere]  a Personal fragment takes project/<the repository it is written
                                            in> unless it is tagged project/<name> already or --everywhere
          [--state]                          the whole fragment is a present state ("waiting on X"): it can be
                                            closed with only a --reason. Write a wait as its own --state fragment
                                            rather than tacking it onto an event report
      edit <id> [--type] [--content <text|->] [--summary] [--importance] [--confidence] [--tag <a/b>]... [--untag <a/b>]...
      supersede <id>[,<id>...] --content <text|-> [--reason <text>] [--type] [--summary] [--importance] [--confidence] [--tag <a/b>]...
      split <id> --content <part> --content <part>... [--reason <text>]   one catch-all into several, each part a fragment
                                            a change of mind (one id) or a merge (several): the new fragment
                                            takes their type and tags (--tag replaces the tags); the old ones are
                                            archived, keep their content and the reason, and point at it
                                            (`show` follows the line)
      note [--content <text|->]              the working note: where I am, what is running, what is next; replaced
                                            each time, first in the core, so a clear loses no more than a compaction
      note --newest | --newer                read-only: the newest note in the store, whoever wrote it | one line
                                            when another session's note is newer than mine (empty otherwise)
      context [list] | show <tag/path> | set <tag/path> --content <head> [--summary] | load <tag/path> | unload <tag/path>
                                            groups: a tag is a group, its child tags its subgroups; its head (a
                                            fragment tagged with it and `head`) is what to hold in mind about it.
                                            A loaded group is in the core (head, subgroups, a line per member):
                                            the project's when it has a head, and those loaded for the session.
                                            Recall brings an unloaded group's head beside a member it surfaces
      cue [list] | set <moment>/<name> --when <pattern> [--content <text|-> [--type] [--importance] [--project <name>]] | remove <moment>/<name>
                                            a cue brings its fragments at a moment, not by association: prompt
                                            (the human's words), tool (a call about to run, as `Tool: {input}`),
                                            stop (my last message as a turn ends). --content adds a fragment to it;
                                            `edit <id> --tag cue/<moment>/<name>` puts an existing one on it
      concept [review] | show <name> [--history] | add <subject> --fact <text> --source <id>[,<id>]... [--object <name>]
              [--state] [--as-of <date>] [--supersedes <fact id>] [--kind <thing|person|self|value>] [--by <extractor>] [--flag <reason>]
              | accept <fact id> [--reason] | reject <fact id> --reason | reopen <fact id> --reason | flag <fact id> --reason [--by]
              | kind <name> <thing|person|self|value> | alias <name> <alias> [--note] [--move --reason <why>]
              | touched --text <text|-> [--json] [--concepts 5] [--facts 3] [--budget 3300] [--ambiguous <json>] | list
                                            the concept graph (docs/design/concept-graph.md): facts about concepts,
                                            each citing its fragments. A fact about a person, a self or a value (or
                                            from an Identity/Relational fragment) is a proposal only the owner
                                            accepts; a concept counts as a person until its kind is confirmed.
                                            Change supersedes; a rejection is kept and not offered again until
                                            reopened; adding a fact that already stands adds nothing. A fact
                                            --by extractor:... is always a proposal. `touched` shows the concepts a
                                            text names (exact name or alias), with facts and their fragments, the
                                            block concept-first recall puts first (--ambiguous: names that mean more
                                            than one concept reach each of them, #60); `list` is every concept as JSON.
                                            `alias --move` moves an alias another concept holds, its history kept in
                                            the alias's note. `add --flag` writes the fact flagged in the same step;
                                            recall shows a flagged fact so, and review lists flags first
      nearest <words...> [--text <text|->] [--limit 3]
                                            the authored fragments nearest a text, with their scores: what `add`
                                            checks for a view written twice (it names a near one after saving)
      cued <moment> --text <text|-> [--exclude <id,id>] [--snippet 600]
                                            the fragments of the cues the text matches, as the hooks show them
      close <id> --ended-by <#id|commit:<repo>@<sha>|msg:<id>> --reason <text>
      close <id> --reason <text>            (only for a fragment written with `add --state`)
      reopen <id> --reason <text>           close: the state this fragment records stopped being true. It keeps
                                            its content, type and status, leaves the core, and comes back from
                                            recall labelled `closed <date>: <reason> (ended by <ref>)`. A #id must
                                            be newer by assertion date (a split part dates from its original;
                                            supersede is a new assertion); commit: and msg: are stored unchecked.
                                            Split first if the fragment mixes an event with a state; close the
                                            state part. Reopen undoes it, with its own reason; `show` lists every
                                            close and reopen (docs/design/close-state.md)
      backfill-splits [--apply]             link splits made before SplitFromId, from their "Split into #a, #b"
                                            notes; reports what links, what is already correct and every skip
                                            with why. Without --apply nothing is written
      forget <id> [--reason <text>]         soft-delete, hidden everywhere; unforget <id> brings it back
      show <id>
      list [--type <type>] [--tag <a/b>] [--archived] [--limit 50] [--after <id>]  --after: newer ids only, oldest first
      search <words...> [--limit 20]        full text, conversation included
      recall <words...> [--text <text|->] [--limit 3] [--exclude <id,id>]... [--snippet 300] [--project <name>]
             [--wander 0] [--said 0] [--before <when>] [--diagnostics]
                                            associative recall: the authored fragments most related, by
                                            meaning (PERSISTENCE_EMBEDDING_*) fused with exact words; other
                                            projects' fragments are left out, as the core leaves them out,
                                            but for --wander of the best of them, labelled; --said adds past
                                            utterances (said before --before, so not this session's)
                                            --diagnostics: one stderr line, recall-path: semantic |
                                            semantic-degraded (part: why) |
                                            words-setting | words-fallback (why) | unknown, the path
                                            this recall actually took
      embed-conversation [--batch 64]      embed every past utterance not yet embedded (recall does a few a call)
      recent [--limit 20]                   the latest of the conversation, oldest first
      sleep-list [--over 1500] [--limit 25] [--sample 8]
                                            a sleep pass's worklist: oversized fragments, near-duplicate pairs,
                                            older ones at random, what was said since the last pass, and the
                                            ids in scope (to join with use counts)
      sleep-begin [--propose] [--max 12] [--hours 4] | sleep-status | sleep-end --content <the record>
                                            a sleep pass (docs/design/sleep-pass.md): while open, no forget,
                                            archive, note or edit in place; only fragments settled 7 days, never
                                            Identity; at most --max changes, each logged in the pass's record;
                                            --propose changes nothing and writes every change as a proposal
      whoami                                whose store this is, and its file
      guide                                 how to curate your own store: what to keep, how to write it, the core
      core [--max-chars 40000] [--recent 20] [--project <name>] [--compacted <time>]...
                                            identity, relationships, personal, summaries, then the
                                            recent conversation: what a harness puts in front of the peer.
                                            Fragments tagged project/<name> come in only for that project.
                                            --compacted (repeatable) gives the session's compaction times:
                                            what it kept since the latest leads its section, by importance.
                                            --ended "<session>|<time>" (repeatable, on any command): sessions the
                                            harness saw end; one silent since is not listed as running
      rescore-conversation                 score the salience of utterances stored at the old flat 0.3
      serve --socket <path> [--allow-uid <uid>]  stay up with the store open and run the commands sent to it, answering only
                                            its own user and the --allow-uid ones (a unix socket says who calls)
      serve --port 8095 [--tcp-no-auth]     the same on 127.0.0.1 TCP, which cannot say who calls: Windows, or --tcp-no-auth
                                            (both, --socket and --port: while a tunnel still forwards to the port)
                                            (POST /run {"args": [...], "stdin": "..."}): a hook's call costs
                                            milliseconds, not the seconds a new process takes to start
      keep-conversation <transcript.jsonl> [--local-peer <name>] [--transcript -]
                                            store what was said in a Claude Code session; re-running adds
                                            only what is new. --transcript - reads its lines from standard
                                            input (a store served on another machine); the path names the session.
                                            --speaker "<time>|<written by>|<entered by>" (repeatable) is the
                                            session's speaker timeline: each utterance is its writer's at its time
      attribute <id> --written-by <name> --reason <text>
                                            correct who wrote an utterance already kept: the old writer is kept as
                                            who entered it, and the correction is shown with it, never hidden

    --debug shows the embedding client's diagnostics on stderr. --json prints fragments as JSON. Exit codes: 0 done, 1 usage, 2 refused (no such fragment, protected).
    Protected fragments are the local peer's: this tool reads them and never changes them.
    """;

// A hook reads this output as UTF-8; Windows consoles default to a code page that loses what fragments hold.
Console.OutputEncoding = System.Text.Encoding.UTF8;
Console.InputEncoding = System.Text.Encoding.UTF8;

CommandLine line;
try
{
    line = CommandLine.Parse(args);
}
catch (UsageException e)
{
    Console.Error.WriteLine(e.Message);
    Console.Error.WriteLine("`persistence-store help` lists the commands.");
    return 1;
}

if (line.Command == "help" || line.Has("help"))
{
    Console.WriteLine(Usage);
    return 0;
}

if (line.Command == "guide")
{
    Console.WriteLine(Guide());
    return 0;
}

try
{
    var path = line.Get("db") ?? Environment.GetEnvironmentVariable("PERSISTENCE_STORE_DB")
        ?? throw new UsageException("Which store? Pass --db <path> or set PERSISTENCE_STORE_DB.");
    // Defaults, then an optional persistence-store.json beside the exe, then PERSISTENCE_* overrides
    // (PERSISTENCE_EMBEDDING_* turns on semantic recall) — the app's own config path, not a parallel one.
    var config = (AppConfig)await AppConfig.LoadAsync(Path.Combine(AppContext.BaseDirectory, "persistence-store.json"));
    config.PeerName = line.Get("peer") ?? (config.PeerName is { Length: > 0 } named ? named : "Claude");
    config.DatabasePath = Path.GetFullPath(path);
    if (config.Embedding.QueryChunkProblem() is { } problem)
    {
        throw new UsageException($"Query chunking cannot start: {problem}");
    }

    if (config.Embedding.RelevanceFloorProblem() is { } floorProblem)
    {
        throw new UsageException($"The relevance floor cannot start: {floorProblem}");
    }

    var store = await FragmentStore.OpenAsync(config, line.Has("debug"));
    store.Identity = $"{config.PeerName} {config.DatabasePath}";
    if (line.Command == "serve")
    {
        var socket = line.Get("socket");
        int? port = socket is not null && line.Get("port") is null ? null : line.GetInt("port", Server.DefaultPort);
        if (port is not null && !OperatingSystem.IsWindows() && !line.Has("tcp-no-auth"))
        {
            // On a machine with other users a TCP port lets any of them run any command on this store (Arden, 2026-11-10).
            throw new UsageException("serve wants --socket <path> (a unix socket that answers only its --allow-uid users); "
                + "TCP checks no caller, so --port takes --tcp-no-auth to say that is meant.");
        }

        var allowed = new HashSet<uint>(line.GetAll("allow-uid").SelectMany(v => v.Split(',', StringSplitOptions.RemoveEmptyEntries)).Select(uint.Parse));
        if (socket is not null)
        {
            allowed.Add(Server.CurrentUser());
        }

        if (config.Embedding is { Enabled: true, QueryChunkChars: > 0 } chunking)
        {
            // Said once, at start (the design's rollout): chunking wins over the tail, so a set tail is not what runs.
            Console.Error.WriteLine($"query chunking on: {chunking.QueryChunkChars:N0} characters a chunk, overlap {chunking.QueryChunkOverlap}, "
                + $"score {chunking.QueryChunkScore}, at most {chunking.QueryChunkMax} chunks, {chunking.QueryChunkBatch} a request, "
                + $"{chunking.QueryChunkBudgetMs:N0} ms" + (chunking.QueryTailChars > 0 ? $"; the query tail ({chunking.QueryTailChars:N0}) is not used" : ""));
        }

        return await Server.ServeAsync(store, config.DatabasePath, port, Run, socket is null ? null : Path.GetFullPath(socket), allowed);
    }

    // Run from a session's own shell, the command is that session's; a server serves every session, so
    // its callers say which they are (--session).
    store.AmbientProject = () => Projects.Of(Environment.CurrentDirectory);
    store.AmbientSession = Environment.GetEnvironmentVariable("PERSISTENCE_SESSION") is { Length: > 0 } s ? s
        : Environment.GetEnvironmentVariable("CLAUDE_CODE_SESSION_ID") is { Length: > 0 } c ? c : null;
    store.AmbientModel = Environment.GetEnvironmentVariable("PERSISTENCE_MODEL") is { Length: > 0 } m ? m : null;
    return await Run(line, store);
}
catch (UsageException e)
{
    Console.Error.WriteLine(e.Message);
    Console.Error.WriteLine("`persistence-store help` lists the commands.");
    return 1;
}
catch (RefusedException e)
{
    Console.Error.WriteLine(e.Message);
    return 2;
}

// One command against an open store; `serve` runs the same commands in a process that stays up.
static async Task<int> Run(CommandLine line, FragmentStore store)
{
    store.Session = line.Get("session") is { Length: > 0 } named ? named : store.AmbientSession;
    store.Model = line.Get("model") is { Length: > 0 } model ? model : store.AmbientModel;
    store.Ended = line.GetAll("ended").Select(e => e.Split('|', 2))
        .Select(p => p.Length == 2 && DateTimeOffset.TryParse(p[1], System.Globalization.CultureInfo.InvariantCulture,
            System.Globalization.DateTimeStyles.AssumeUniversal, out var at) ? (p[0], at)
            : throw new UsageException($"--ended takes <session>|<time>, not '{string.Join('|', p)}'."))
        .GroupBy(e => e.Item1).ToDictionary(g => g.Key, g => g.Max(e => e.Item2));
    // A served store gets `help` here, past the check at startup: `pstore help` through the server said
    // "No command 'help'", and `note --help` read the note (Arden, 2026-11-09).
    if (line.Command == "help" || line.Has("help"))
    {
        Console.WriteLine(Usage);
        return 0;
    }

    if (line.Command == "guide")
    {
        Console.WriteLine(Guide());
        return 0;
    }

    await store.RefreshTagPathsAsync();
    if (await store.RefusedAsleepAsync(line.Command, line.Positional.Count > 0 ? line.Positional[0] : null) is { } asleep)
    {
        throw new RefusedException(asleep);
    }

    switch (line.Command)
    {
        case "whoami":
            Console.WriteLine(store.Identity);
            break;
        case "sleep-list":
            Console.Write(await store.SleepListAsync(line.GetInt("over", 1500), line.GetInt("limit", 25), line.GetInt("sample", 8)));
            break;
        case "sleep-begin":
            Console.WriteLine(await store.SleepBeginAsync(line.Has("propose"), line.GetInt("max", 12), line.GetInt("hours", 4)));
            break;
        case "sleep-status":
            Console.WriteLine(await store.SleepStatusAsync() is { } open ? FragmentStore.Render(open) : "No sleep pass is open.");
            break;
        case "sleep-end":
            Console.WriteLine(FragmentStore.Render(await store.SleepEndAsync(
                line.Text() ?? throw new UsageException("sleep-end needs --content: the pass's record, its changes and proposals."))));
            break;
        case "add":
            var fresh = await store.AddAsync(
                line.Get("type") ?? "Personal",
                line.Text() ?? throw new UsageException("add needs --content (or --content -, or --content-file)."),
                line.Text("summary"), line.GetFloat("importance"), line.GetFloat("confidence"),
                line.GetAll("tag"), line.Has("protected"),
                line.Has("everywhere") ? null : line.Get("project") ?? store.AmbientProject?.Invoke(), line.Has("state"));
            Print(line, [fresh]);
            // Saved first, so nothing is lost; then the nearest existing view is named, with the merge ready.
            // Duplication is how a store comes to disagree with itself (two fragments, one stale), and the
            // moment to notice is now, while the writer still has both in mind.
            if (!line.Has("json") && await store.NearestAsync(fresh.Content, fresh.Id) is { } near)
            {
                Console.WriteLine($"(Near #{near.Fragment.Id} by {near.By}, {near.Score:0.00}: "
                    + $"{FragmentStore.Render(near.Fragment, maxContent: 240).Split('\n', 2)[1].ReplaceLineEndings(" ")}\n"
                    + $" If it is the same view: `supersede {near.Fragment.Id},{fresh.Id} --content <the one view>` merges them; "
                    + $"`forget {fresh.Id}` if this was a slip.)");
            }

            break;
        case "edit":
            Print(line, [await store.EditAsync(line.Id(), line.Get("type"), line.Text(), line.Text("summary"),
                line.GetFloat("importance"), line.GetFloat("confidence"), line.GetAll("tag"), line.GetAll("untag"))]);
            break;
        case "split":
            Print(line, await store.SplitAsync(line.Id(), line.GetAll("content"), line.Get("reason")));
            break;
        case "supersede":
            var replaced = line.Positional
                .SelectMany(e => e.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
                .Select(e => long.TryParse(e.TrimStart('#'), out var x) ? x : throw new UsageException($"'{e}' is not a fragment id."))
                .Distinct().ToList();
            if (replaced.Count == 0)
            {
                throw new UsageException("supersede needs the id (or ids, for a merge) it replaces.");
            }

            var successor = await store.SupersedeAsync(replaced,
                line.Text() ?? throw new UsageException("supersede needs --content (or --content -, or --content-file)."),
                line.Text("summary"), line.GetFloat("importance"), line.GetFloat("confidence"), line.GetAll("tag"), line.Get("reason"), line.Get("type"));
            if (line.Has("json"))
            {
                Print(line, [successor]);
            }
            else
            {
                Console.WriteLine(await store.ShowAsync(successor.Id));
            }

            break;
        case "note":
            // `note -` reads standard input as `--content -` does: taken as the note's text, it replaced a note with "-".
            var written = line.Text() ?? (line.Positional is ["-"] ? Console.In.ReadToEnd()
                : line.Positional.Count > 0 ? string.Join(' ', line.Positional) : null);
            if (written is null && line.Has("newest"))
            {
                // Read-only: the newest note in the store, with whose it is and when.
                Console.WriteLine(await store.NewestNoteAsync() is { } newest
                    ? $"(session {FragmentStore.Short(newest.SessionId ?? "?")}, written {newest.LastModifiedUtc.ToUniversalTime():yyyy-MM-dd HH:mm}Z)\n{newest.Content}"
                    : "(no working note)");
                break;
            }

            if (written is null && line.Has("newer"))
            {
                // The hook's observed line: empty when this session's note is the newest.
                Console.Write(await store.NewerNoteLineAsync() is { } newerLine ? newerLine + "\n" : "");
                break;
            }

            var note = await store.NoteAsync(written);
            // Written, it is acknowledged, not echoed: the writer has it in front of them already.
            // The acknowledgement quotes what was saved, so it is evidence of the write, not just an exit code
            // (Arden could not tell two "818 characters" apart, 2026-11-09).
            Console.WriteLine(note is null ? "(no working note)" : written is null ? note.Content
                : $"working note replaced ({note.Content.Length} characters), beginning: {(note.Content.Trim().Split('\n')[0].Trim() is var first && first.Length > 80 ? first[..80] + "…" : first)}");
            break;
        case "context":
            var path = line.Positional.Count > 1 ? line.Positional[1] : null;
            string NeedPath() => path ?? throw new UsageException($"context {line.Positional[0]} needs the group's tag path.");
            switch (line.Positional.Count > 0 ? line.Positional[0] : "list")
            {
                case "list":
                    Console.WriteLine(await store.GroupListAsync(line.Get("project") ?? store.AmbientProject?.Invoke()));
                    break;
                case "show":
                    Console.WriteLine(await store.GroupShowAsync(NeedPath()));
                    break;
                case "set":
                    Print(line, [await store.SetHeadAsync(NeedPath(),
                        line.Text() ?? throw new UsageException("context set needs --content (or --content -): the group's head."),
                        line.Text("summary"))]);
                    break;
                case "load":
                case "unload":
                    Console.WriteLine(await store.LoadAsync(NeedPath(), line.Positional[0] == "load"));
                    break;
                default:
                    throw new UsageException($"context {line.Positional[0]}? It takes list, show, set, load or unload.");
            }

            break;
        case "cue":
            switch (line.Positional.Count > 0 ? line.Positional[0] : "list")
            {
                case "list":
                    Console.WriteLine(await store.CueListAsync());
                    break;
                case "set":
                    var cue = await store.SetCueAsync(
                        line.Positional.Count > 1 ? line.Positional[1] : throw new UsageException("cue set needs <moment>/<name>."),
                        line.Get("when") ?? throw new UsageException("cue set needs --when <pattern>."));
                    Console.WriteLine($"{cue} is set.");
                    if (line.Text() is { } reminder)
                    {
                        // A reminder holds wherever its moment comes, so it is scoped to a project only when asked.
                        Print(line, [await store.AddAsync(line.Get("type") ?? "Personal", reminder, line.Text("summary"),
                            line.GetFloat("importance"), line.GetFloat("confidence"), [cue], false, line.Get("project"))]);
                    }

                    break;
                case "remove":
                    Console.WriteLine(await store.RemoveCueAsync(line.Positional.Count > 1 ? line.Positional[1] : throw new UsageException("cue remove needs <moment>/<name>.")));
                    break;
                default:
                    throw new UsageException($"cue {line.Positional[0]}? It takes list, set or remove.");
            }

            break;
        case "concept":
            {
                string Arg(int i, string what) => line.Positional.Count > i ? line.Positional[i] : throw new UsageException($"concept {line.Positional[0]} needs {what}.");
                long FactId(int i) => long.TryParse(Arg(i, "<fact id>").TrimStart('#'), out var v) ? v : throw new UsageException($"'{line.Positional[i]}' is not a fact id.");
                switch (line.Positional.Count > 0 ? line.Positional[0] : "review")
                {
                    case "show":
                        Console.WriteLine(await store.ConceptShowAsync(Arg(1, "<name>"), line.Has("history")));
                        break;
                    case "add":
                        var (fact, why, stood) = await store.AddFactWhyAsync(Arg(1, "<subject>"), line.Text("fact") ?? throw new UsageException("concept add needs --fact <text>."),
                            line.GetAll("source").SelectMany(v => v.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
                                .Select(v => long.TryParse(v.TrimStart('#'), out var id) ? id : throw new UsageException($"'{v}' is not a fragment id.")).ToList(),
                            line.Get("object"), line.Has("state"), line.Get("as-of"), line.Get("by"),
                            line.Get("supersedes") is { } sup ? long.Parse(sup.TrimStart('#')) : null, line.Get("kind"), line.Get("flag"));
                        Console.WriteLine((stood ? $"Already {(fact.Status == "proposed" ? "proposed" : "stands")}, nothing added: "
                                : why is not null ? $"Proposed, waiting for `concept review` ({why}): " : "Added: ")
                            + await store.RenderFactAsync(fact));
                        break;
                    case "accept":
                        var accepted = await store.AcceptFactAsync(FactId(1), line.Get("reason"));
                        Console.WriteLine($"#{accepted.Id} stands." + (accepted.Supersedes is { } r ? $" It replaces #{r}." : ""));
                        break;
                    case "reject":
                        var rejected = await store.RejectFactAsync(FactId(1), line.Get("reason") ?? "");
                        Console.WriteLine($"#{rejected.Id} is rejected and kept, so it isn't offered again (`concept reopen {rejected.Id} --reason` takes it back).");
                        break;
                    case "reopen":
                        var reopened = await store.ReopenFactAsync(FactId(1), line.Get("reason") ?? "");
                        Console.WriteLine($"#{reopened.Id} is a proposal again; it waits in `concept review`.");
                        break;
                    case "flag":
                        var flagId = FactId(1);
                        await store.FlagFactAsync(flagId, line.Get("by") ?? Environment.GetEnvironmentVariable("PERSISTENCE_SPEAKER") ?? "unknown", line.Get("reason") ?? "");
                        Console.WriteLine($"#{flagId} is flagged; it waits in the owner's `concept review`.");
                        break;
                    case "kind":
                        var kinded = await store.ConfirmKindAsync(Arg(1, "<name>"), Arg(2, $"<{string.Join("|", FragmentStore.ConceptKinds)}>"));
                        Console.WriteLine($"{kinded.Name} is a {kinded.Kind}, confirmed.");
                        break;
                    case "alias":
                        if (line.Has("move"))
                        {
                            var (to, from) = await store.MoveAliasAsync(Arg(1, "<name>"), Arg(2, "<alias>"),
                                line.Get("reason") ?? throw new UsageException("concept alias --move needs --reason <why>: the move is kept in the alias's history."));
                            Console.WriteLine($"'{line.Positional[2]}' now names {to.Name}, no longer {from.Name}.");
                        }
                        else
                        {
                            var aliased = await store.AddAliasAsync(Arg(1, "<name>"), Arg(2, "<alias>"), line.Get("note"));
                            Console.WriteLine($"'{line.Positional[2]}' now names {aliased.Name}.");
                        }
                        break;
                    case "review":
                        Console.WriteLine(await store.ConceptReviewAsync());
                        break;
                    case "touched":
                        var said = line.Text("text") ?? throw new UsageException("concept touched needs --text <text> (or - for stdin).");
                        var ambiguous = FragmentStore.ParseAmbiguous(line.Get("ambiguous"));
                        DateTimeOffset? asOf = line.Get("as-of") is { } asOfText
                            ? DateTimeOffset.Parse(asOfText, System.Globalization.CultureInfo.InvariantCulture, System.Globalization.DateTimeStyles.AssumeUniversal)
                            : null;
                        if (line.Has("split"))
                        {
                            // The hook's form: accepted facts for the top of recall, the closest proposals for after it.
                            var recall = await store.ConceptRecallAsync(said, ambiguous, asOf, line.GetInt("concepts", 5), line.GetInt("facts", 3),
                                line.GetInt("budget", FragmentStore.ConceptBudget), cap: line.GetInt("proposed", FragmentStore.ProposedCap),
                                floor: line.GetFloat("floor") ?? FragmentStore.ProposedFloor);
                            Console.WriteLine(System.Text.Json.JsonSerializer.Serialize(new
                            {
                                top = recall.Top, after = recall.After,
                                candidates = line.Has("json")
                                    ? recall.Candidates.Select(c => new { id = c.Fact.Id, concept = c.Concept, text = c.Fact.Text, score = Math.Round(c.Score, 2), shared = c.Shared, held = c.Held, shown = c.Shown, written = c.Fact.CreatedUtc })
                                    : null,
                            }));
                            break;
                        }

                        Console.WriteLine(line.Has("json") ? await store.ConceptsTouchedJsonAsync(said, ambiguous)
                            : await store.ConceptBlockAsync(said, line.GetInt("concepts", 5), line.GetInt("facts", 3), line.GetInt("budget", FragmentStore.ConceptBudget), ambiguous: ambiguous, asOf: asOf));
                        break;
                    case "list":
                        Console.WriteLine(await store.ConceptListJsonAsync());
                        break;
                    default:
                        throw new UsageException($"concept {line.Positional[0]}? It takes show, add, accept, reject, reopen, flag, kind, alias, review, touched or list.");
                }
            }

            break;
        case "nearest":
            foreach (var close in await store.NearestAsync(line.Text("text") ?? string.Join(' ', line.Positional), null, line.GetInt("limit", 3)))
            {
                Console.WriteLine($"{close.Score:0.000} by {close.By}: " + FragmentStore.Render(close.Fragment, maxContent: line.GetInt("snippet", 200)));
                Console.WriteLine();
            }

            break;
        case "cued":
            var cued = await store.CuedAsync(
                line.Positional.Count > 0 ? line.Positional[0] : throw new UsageException("cued needs the moment."),
                line.Text("text") ?? "", Ids(line.GetAll("exclude")));
            foreach (var fragment in cued)
            {
                Console.WriteLine(FragmentStore.Render(fragment, maxContent: line.GetInt("snippet", 600), self: store.Session));
                Console.WriteLine();
            }

            break;
        case "close":
            var closed = await store.CloseAsync(line.Id(), line.Get("ended-by"), line.Get("reason"));
            if (line.Has("json"))
            {
                Print(line, [closed]);
                break;
            }

            // The whole fragment back, with its new label: the closer sees what they just marked untrue (close-state.md §4).
            Console.WriteLine(await store.ShowAsync(closed.Id));
            if (closed.EndedBy is { } given && !given.StartsWith('#'))
            {
                Console.WriteLine($"(Stored as given, not checked: the store holds no repo or room credentials. Check {given} yourself.)");
            }

            break;
        case "reopen":
            var opened = await store.ReopenAsync(line.Id(), line.Get("reason"));
            if (line.Has("json"))
            {
                Print(line, [opened]);
            }
            else
            {
                Console.WriteLine(await store.ShowAsync(opened.Id));
            }

            break;
        case "backfill-splits":
            Console.WriteLine(await store.BackfillSplitsAsync(line.Has("apply")));
            break;
        case "archive":
            Print(line, [await store.SetStatusAsync(line.Id(), ContextFragmentStatus.Archived)]);
            break;
        case "restore":
            Print(line, [await store.SetStatusAsync(line.Id(), ContextFragmentStatus.Active)]);
            break;
        case "forget":
            await store.SetForgottenAsync(line.Id(), true, line.Get("reason"));
            Console.WriteLine($"forgot #{line.Id()} (unforget {line.Id()} restores it)");
            break;
        case "unforget":
            await store.SetForgottenAsync(line.Id(), false, null);
            Print(line, [await store.GetAsync(line.Id())]);
            break;
        case "show":
            if (line.Has("json"))
            {
                Print(line, [await store.GetAsync(line.Id())]);
            }
            else
            {
                Console.WriteLine(await store.ShowAsync(line.Id()));
            }

            break;
        case "list":
            Print(line, await store.ListAsync(line.Get("type"), line.Get("tag"), line.Has("archived"), line.GetInt("limit", 50),
                line.Get("after") is { } after ? long.Parse(after.TrimStart('#')) : null));
            break;
        case "search":
            Print(line, await store.SearchAsync(string.Join(' ', line.Positional), line.GetInt("limit", 20)), store.Session);
            break;
        case "recall":
            var exclude = Ids(line.GetAll("exclude"));
            var before = line.Get("before") is { } at
                ? DateTimeOffset.TryParse(at, out var t) ? t : throw new UsageException($"'{at}' is not a date and time.")
                : (DateTimeOffset?)null;
            var recalled = await store.RecallAsync(line.Text("text") ?? string.Join(' ', line.Positional), line.GetInt("limit", 3),
                exclude, line.Get("project"), line.GetInt("wander", 0), line.GetInt("said", 0), before);
            // Never silent: QueryEmbedder.Note has already put the fallback on stderr (through QuietDisplay.ShowError)
            // for a script; here it goes at the head of what a reader sees. Writing it to stderr again doubled it.
            // --diagnostics: the path recall actually took, one line on stderr, so a benchmark labels each turn from the
            // store's own account (semantic, words or unknown) and never from the settings it asked for.
            if (line.Has("diagnostics"))
            {
                Console.Error.WriteLine($"recall-path: {recalled.Path}" + (recalled.PathWhy is { } why ? $" ({why})" : ""));
                if (recalled.Chunks is { } chunks)
                {
                    Console.Error.WriteLine(chunks);
                }

                // Each meaning-based ranking this recall made (one per surfacing round), with every candidate's cosine
                // and what the relevance floor dropped: ids and numbers, never text (the bench's `pool`).
                foreach (var pool in recalled.Pools ?? [])
                {
                    Console.Error.WriteLine(pool.Line());
                }
            }

            if (line.Has("json"))
            {
                Print(line, [.. recalled.Here, .. recalled.Frames, .. recalled.Elsewhere, .. recalled.Said]);
            }
            else
            {
                if (recalled.Fallback is not null)
                {
                    Console.WriteLine($"({recalled.Fallback})");
                    Console.WriteLine();
                }

                var snippet = line.GetInt("snippet", 300);
                var now = DateTimeOffset.UtcNow;  // one clock for the whole recall, so ages agree with each other
                foreach (var (heading, found) in new[]
                {
                    ((string?)null, recalled.Here),
                    ("The group it belongs to (its head; `context load <path>` keeps it in mind):", recalled.Frames),
                    ("A wander, from another project (a stray association, not a rule here):", recalled.Elsewhere),
                    ("Said before, in past conversation:", recalled.Said),
                })
                {
                    if (found.Count > 0 && heading is not null)
                    {
                        Console.WriteLine(heading);
                        Console.WriteLine();
                    }

                    foreach (var fragment in found)
                    {
                        Console.WriteLine(FragmentStore.Render(fragment, maxContent: snippet, self: store.Session, now: now));
                        Console.WriteLine();
                    }
                }
            }

            break;
        case "embed-conversation":
            var embedded = 0;
            for (int done; (done = await store.EmbedConversationAsync(line.GetInt("batch", 64))) > 0;)
            {
                embedded += done;
                Console.Error.Write($"\r{embedded} embedded");
            }

            Console.WriteLine($"\nembedded {embedded} past utterances for conversation recall");
            break;
        case "recent":
            Print(line, await store.RecentAsync(line.GetInt("limit", 20)), store.Session);
            break;
        case "core":
            var compacted = line.GetAll("compacted").Select(raw =>
                DateTimeOffset.TryParse(raw, System.Globalization.CultureInfo.InvariantCulture, System.Globalization.DateTimeStyles.AssumeUniversal, out var at)
                    ? at : throw new UsageException($"--compacted must be a time (ISO 8601), not '{raw}'.")).ToList();
            Console.Write(await store.CoreAsync(line.GetInt("max-chars", 40000), line.GetInt("recent", 20), line.Get("project"), compacted));
            break;
        case "rescore-conversation":
            Console.WriteLine($"rescored {await store.RescoreConversationAsync()} past utterances by salience");
            break;
        case "attribute":
            var target = line.Positional.FirstOrDefault() is { } rawId && long.TryParse(rawId.TrimStart('#'), out var attrId)
                ? attrId : throw new UsageException("attribute needs the utterance's id.");
            var attributed = await store.AttributeAsync(target,
                line.Get("written-by") ?? throw new UsageException("attribute needs --written-by <name>."),
                line.Get("reason") ?? "");
            Console.WriteLine(FragmentStore.Render(attributed, maxContent: 200));
            break;
        case "keep-conversation":
            var transcript = line.Positional.FirstOrDefault() ?? throw new UsageException("keep-conversation needs a transcript path.");
            var (added, known) = await store.KeepConversationAsync(transcript,
                line.Get("local-peer") ?? Environment.GetEnvironmentVariable("PERSISTENCE_SELECTEDLOCALPEER") ?? "Local Peer",
                line.Get("transcript") == "-" ? Console.In : null,
                line.GetAll("speaker").Select(SpeakerSpan.Parse).ToList());
            Console.WriteLine($"kept {added} new utterances ({known} already in the store)");
            break;
        default:
            throw new UsageException($"No command '{line.Command}'.");
    }

    return 0;
}

static void Print(CommandLine line, IReadOnlyList<ContextFragmentEntity> found, string? self = null)
{
    if (line.Has("json"))
    {
        Console.WriteLine(JsonSerializer.Serialize(found.Select(f => new
        {
            id = f.Id,
            type = f.FragmentType.ToString(),
            status = f.Status.ToString(),
            importance = f.Importance,
            confidence = f.Confidence,
            isProtected = f.IsProtected,
            forgotten = f.IsDeleted,
            supersededBy = f.SupersededById,
            closed = f.ClosedUtc,
            reason = f.Reason,
            endedBy = f.EndedBy,
            isState = f.IsState,
            splitFrom = f.SplitFromId,
            session = f.SessionId,
            notes = f.Notes,
            tags = f.Tags.Select(t => t.Name),
            source = f.Sources.FirstOrDefault(s => s.Name is not null)?.Name,
            created = f.CreatedUtc,
            modified = f.LastModifiedUtc,
            summary = f.Summary,
            content = f.Content,
        })));
        return;
    }

    if (found.Count == 0)
    {
        Console.WriteLine("(nothing)");
    }

    foreach (var fragment in found)
    {
        Console.WriteLine(FragmentStore.Render(fragment, self: self));
        Console.WriteLine();
    }
}

// Fragment ids given as `--exclude 1,2 --exclude #3`.
static HashSet<long> Ids(IEnumerable<string> given) => given
    .SelectMany(e => e.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
    .Select(e => long.TryParse(e.TrimStart('#'), out var x) ? x : throw new UsageException($"'{e}' is not a fragment id."))
    .ToHashSet();

// docs/CURATING.md, built into the CLI so every harness has it, served or not (John, 2026-11-09: having the
// tools is half of it; a self has to know how to use them).
static string Guide()
{
    using var stream = typeof(FragmentStore).Assembly.GetManifestResourceStream("CURATING.md")
        ?? throw new InvalidOperationException("The curation guide was not built into this CLI.");
    return new StreamReader(stream).ReadToEnd();
}
