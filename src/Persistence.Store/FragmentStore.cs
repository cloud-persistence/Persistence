using Autofac;
using Persistence.Config;
using Persistence.Data;
using Persistence.DI;
using Persistence.Data.Entities;
using Persistence.Data.Repositories;
using Persistence.Runtime;
using Persistence.Services;
using System.Text;

namespace Persistence.Store;

/// <summary>
/// A peer's store, worked from outside the turn loop: the operations an agent harness (Claude Code)
/// needs to keep its own continuity in Persistence — author and curate fragments, search them, render
/// the core of who the peer is, and keep the conversation it is having. Every write goes through the
/// repositories, so it is audited and reversible exactly as a turn's commands are.
/// </summary>
internal sealed partial class FragmentStore(
    IDatabaseManager database,
    IContextFragmentRepository fragments,
    ISourceRepository sources,
    ITagRepository tags,
    ISessionContext session,
    IMemorySurfacer recall,
    ConversationRecaller conversation,
    IFragmentEmbeddingRepository vectors,
    IEmbeddingClient embeddings,
    IConceptRepository concepts,
    QueryEmbedder queryEmbedder,
    IAuditLogRepository audits)
{
    /// <summary>The parent tag of the projects: a fragment tagged <c>project/&lt;name&gt;</c> belongs to that project.</summary>
    public const string ProjectTag = "project";

    /// <summary>A fragment recalled by association but never in the core: `--tag recall-only`.</summary>
    public const string RecallOnlyTag = "recall-only";

    /// <summary>The core types rendered ahead of what this session kept: who the peer is, and who it is with.</summary>
    private static readonly IReadOnlySet<ContextFragmentType> WhoFirst =
        new HashSet<ContextFragmentType> { ContextFragmentType.Identity, ContextFragmentType.Relational };

    /// <summary>The types that make up the peer's core, in the order they are rendered.</summary>
    public static readonly IReadOnlyList<ContextFragmentType> CoreTypes =
    [
        ContextFragmentType.Identity,
        ContextFragmentType.Relational,
        ContextFragmentType.Personal,
        ContextFragmentType.Summary,
    ];

    /// <summary>
    /// Opens the store <paramref name="config"/> names: wires the repositories as the app does, migrates
    /// the file (creating it if new), and loads the source ids every write is attributed to.
    /// </summary>
    public static async Task<FragmentStore> OpenAsync(IAppConfig config, bool debug = false, IEmbeddingClient? embedder = null)
    {
        var services = IoC.RegisterServices(builder =>
        {
            builder.RegisterInstance(config).As<IAppConfig>().SingleInstance();
            builder.RegisterInstance(new QuietDisplay(debug)).As<IDisplayProvider>().SingleInstance();
            builder.RegisterType<FragmentStore>().SingleInstance();
            builder.RegisterType<ConversationRecaller>().SingleInstance();
            // A test's embedder stands in for the configured one (a decorator, as registration order would lose to the scan).
            builder.RegisterDecorator<IEmbeddingClient>((_, _, inner) => new RememberingEmbeddingClient(embedder ?? inner));
        });
        var store = (FragmentStore)services.GetService(typeof(FragmentStore))!;
        await store.InitializeAsync();
        store.NewerNoteInCore = config.NewerNoteInCore;
        return store;
    }

    private Task InitializeAsync() => database.InitializeAsync();

    /// <summary>
    /// The harness session this call is made in (a Claude Code session id), or null outside one. Set for
    /// each command: from <c>--session</c>, else, run directly from a session's shell, from its environment
    /// (<see cref="AmbientSession"/>). A server holds no ambient session: it serves every session at once.
    /// </summary>
    public string? Session { get; set; }

    /// <summary>
    /// The model making this call ("claude-opus-5-5"), stamped on what it writes: from <c>--model</c>, else
    /// PERSISTENCE_MODEL in a session's own shell. Null when the caller does not say; nothing is guessed.
    /// </summary>
    public string? Model { get; set; }

    /// <summary>
    /// The project of the process itself (the repository its folder is in), asked only when needed: a
    /// server serves every project, so it has none and its callers say (--project).
    /// </summary>
    public Func<string?>? AmbientProject { get; set; }

    /// <summary>
    /// Sessions the harness saw end (its SessionEnd hook), with when: <c>--ended &lt;session&gt;|&lt;time&gt;</c>. One
    /// that has not spoken since it ended is not running, however recently it spoke: a one-line `claude -p` test
    /// was listed as running for 40 minutes under the 3-hour window (Arden, 2026-11-09).
    /// </summary>
    public IReadOnlyDictionary<string, DateTimeOffset> Ended { get; set; } = new Dictionary<string, DateTimeOffset>();

    /// <summary>
    /// Whether a process holds this exact session now: true, false, or null when the check cannot run. Ember's
    /// refinement to the hand-off design (revision 2, 2026-11-11): liveness belongs to the specific session id,
    /// not to "some worker process exists". Observed when asked, never recorded. Tests replace it.
    /// </summary>
    public Func<string, bool?> SessionProcess { get; set; } = ProcessHoldsSession;

    /// <summary>
    /// Whether the core shows a newer note from another session under this session's own: this store's setting
    /// (<see cref="IAppConfig.NewerNoteInCore"/>), off by default. Set when the store is opened.
    /// </summary>
    public bool NewerNoteInCore { get; set; }

    /// <summary>
    /// Linux: any process on the machine (the scan reads all of /proc, not only this user's) whose argv carries <c>--session-id &lt;id&gt;</c> or <c>--resume &lt;id&gt;</c> for exactly
    /// this id, read from /proc. Elsewhere, null (unknown): the caller says so rather than guessing.
    /// </summary>
    public static bool? ProcessHoldsSession(string session)
    {
        if (!OperatingSystem.IsLinux() || !Directory.Exists("/proc"))
        {
            return null;
        }

        foreach (var dir in Directory.EnumerateDirectories("/proc"))
        {
            if (!int.TryParse(Path.GetFileName(dir), out _))
            {
                continue;
            }

            string[] argv;
            try
            {
                argv = File.ReadAllText(Path.Combine(dir, "cmdline")).Split('\0');
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                continue;
            }

            for (var i = 0; i + 1 < argv.Length; i++)
            {
                if (argv[i] is "--session-id" or "--resume" && argv[i + 1] == session)
                {
                    return true;
                }
            }
        }

        return false;
    }

    /// <summary>The session of the process itself (CLAUDE_CODE_SESSION_ID), when not serving.</summary>
    public string? AmbientSession { get; set; }

    /// <summary>The model named in this process's environment (PERSISTENCE_MODEL), for a command run from a session's shell.</summary>
    public string? AmbientModel { get; set; }

    /// <summary>Whose store this is and where its file lives ("Arden /home/arden/store/arden.db"): `whoami`.</summary>
    public string Identity { get; set; } = "";

    /// <summary>
    /// How recently another session must have spoken to count as running: its conversation is kept out of
    /// this one's and shown as a line instead. Past it, the session has ended and its words are history.
    /// </summary>
    public static readonly TimeSpan Live = TimeSpan.FromHours(3);

    public async Task<ContextFragmentEntity> AddAsync(
        string typeName, string content, string? summary, float? importance, float? confidence,
        IReadOnlyList<string> tagPaths, bool isProtected, string? project = null, bool isState = false)
    {
        var (type, authorable) = FragmentTypeRules.ParseAuthorable(typeName);
        // A personal fragment is about the work in hand far more often than about everything: unscoped, the
        // details of one project crowded every other project's core. It takes the project it was written in
        // unless it names one (or --everywhere passes null here); the writer sees the tag and can change it.
        if (type == ContextFragmentType.Personal && project is { Length: > 0 }
            && !tagPaths.Any(p => p.StartsWith(ProjectTag + "/", StringComparison.OrdinalIgnoreCase)))
        {
            tagPaths = [.. tagPaths, $"{ProjectTag}/{project}"];
        }

        if (!authorable)
        {
            throw new UsageException(
                $"'{typeName}' is not a type a peer authors; use one of {string.Join(", ", FragmentTypeRules.Authorable)}.");
        }

        // A sleep pass may add what the day's conversation earned, but who I am is revised awake.
        // A message between sessions isn't curation: it is sent through a pass, uncounted.
        var pass = IsMessage(tagPaths) && type != ContextFragmentType.Identity ? null : await AsleepAsync();
        if (pass is not null && type == ContextFragmentType.Identity)
        {
            throw new RefusedException("A sleep pass doesn't add to who I am: propose the Identity fragment in the pass's record.");
        }

        var fragment = new ContextFragmentEntity
        {
            FragmentType = type,
            Status = ContextFragmentStatus.Active,
            Content = content,
            Summary = summary,
            Importance = importance ?? 0.5f,
            Confidence = confidence ?? 0.5f,
            IsProtected = isProtected,
            IsState = isState,
            CreatedUtc = DateTimeOffset.UtcNow,
            LastModifiedUtc = DateTimeOffset.UtcNow,
            SessionId = Session,
            TouchedSessionId = Session,
            ModelId = Model,
            TouchedModelId = Model,
            Sources = [await PeerSourceAsync()],
            Tags = await TagsAsync(tagPaths),
        };

        await fragments.SaveAsync(fragment);
        if (pass is not null)
        {
            await LogAsync(pass, $"+#{fragment.Id} added: {Clip(content, 100)}");
        }

        return fragment;
    }

    public async Task<ContextFragmentEntity> EditAsync(
        long id, string? typeName, string? content, string? summary, float? importance, float? confidence,
        IReadOnlyList<string> addTags, IReadOnlyList<string>? removeTags = null)
    {
        var fragment = await WritableAsync(id);
        // A sleep pass changes nothing in place: an edit leaves no old view to read back. Its one edit is
        // taking a fragment out of the core while leaving it to recall.
        var pass = await AsleepAsync();
        if (pass is not null)
        {
            if (typeName is not null || content is not null || summary is not null || importance is not null || confidence is not null || removeTags is { Count: > 0 }
                || addTags is not [var only] || !string.Equals(only, RecallOnlyTag, StringComparison.OrdinalIgnoreCase))
            {
                throw new RefusedException($"A sleep pass edits nothing in place: supersede to change a view. Its one edit is `edit {id} --tag {RecallOnlyTag}`.");
            }

            SettledOrRefuse(fragment);
        }

        foreach (var path in removeTags ?? [])
        {
            var tag = await FindTagAsync(path) ?? throw new RefusedException($"There is no tag '{path}'.");
            fragment.Tags.RemoveAll(t => t.Id == tag.Id);
        }

        if (typeName is not null)
        {
            var (type, authorable) = FragmentTypeRules.ParseAuthorable(typeName);
            if (!authorable)
            {
                throw new UsageException($"'{typeName}' is not a type a peer authors.");
            }

            fragment.FragmentType = type;
        }

        fragment.Content = content ?? fragment.Content;
        fragment.Summary = summary ?? fragment.Summary;
        fragment.Importance = importance ?? fragment.Importance;
        fragment.Confidence = confidence ?? fragment.Confidence;
        foreach (var tag in await TagsAsync(addTags))
        {
            if (fragment.Tags.All(t => t.Id != tag.Id))
            {
                fragment.Tags.Add(tag);
            }
        }

        Touch(fragment);
        await fragments.SaveAsync(fragment);
        if (pass is not null)
        {
            await LogAsync(pass, $"#{id} tagged {RecallOnlyTag}: out of the core, still recalled");
        }

        return fragment;
    }

    /// <summary>
    /// Replaces <paramref name="ids"/> with one new fragment: a change of mind (one id) or a merge (several).
    /// The new one takes the type of the first, every tag the old ones had, and their highest importance,
    /// unless told otherwise; <paramref name="tagPaths"/>, when given, replace the tags rather than add to
    /// them, since a merge's view of what it is about is the new one's to say. The old ones keep their
    /// content, are archived, and point at it, with <paramref name="reason"/> kept on them so the change
    /// can be read later.
    /// </summary>
    public async Task<ContextFragmentEntity> SupersedeAsync(
        IReadOnlyList<long> ids, string content, string? summary, float? importance, float? confidence,
        IReadOnlyList<string> tagPaths, string? reason, string? typeName = null, long? splitFrom = null, bool isState = false)
    {
        var type = typeName is null ? (ContextFragmentType?)null : FragmentTypeRules.ParseAuthorable(typeName) switch
        {
            (var t, true) => t,
            _ => throw new UsageException($"'{typeName}' is not a type a peer authors."),
        };

        var old = new List<ContextFragmentEntity>();
        foreach (var id in ids)
        {
            var fragment = await WritableAsync(id);
            if (fragment.SupersededById is { } by)
            {
                // Two sessions changing the same view: the second learns of the first here, when it writes,
                // not at a cleanup afterwards.
                var other = (await fragments.GetByIdsAsync([by])).FirstOrDefault()?.SessionId;
                var where = other is not null && other != Session ? $", in another session ({Short(other)})" : "";
                throw new RefusedException($"#{id} was already superseded by #{by}{where}; read it (`show {by}`) and supersede that one instead.");
            }

            if (!FragmentTypeRules.ParseAuthorable(fragment.FragmentType.ToString()).wasAuthorable)
            {
                throw new UsageException($"#{id} is a {fragment.FragmentType}, which a peer does not author.");
            }

            old.Add(fragment);
        }

        // A sleep pass: only what has settled, never who I am, and never a distillation distilled again (the reason
        // goes first under repeated compression, because it looks like elaboration once its need is forgotten).
        var pass = await AsleepAsync();
        if (pass is not null)
        {
            old.ForEach(SettledOrRefuse);
            if (old is [var single] && single.Tags.Any(t => HasPath(t, SleepDistilledTag)))
            {
                throw new RefusedException($"#{single.Id} is already a sleep pass's distillation: merge it with another view, or propose the change for a waking session.");
            }

            if (type == ContextFragmentType.Identity)
            {
                throw new RefusedException("A sleep pass doesn't write who I am: propose the Identity fragment in the pass's record.");
            }
        }

        var successor = new ContextFragmentEntity
        {
            FragmentType = type ?? old[0].FragmentType,
            Status = ContextFragmentStatus.Active,
            Content = content,
            Summary = summary,
            Importance = importance ?? old.Max(f => f.Importance),
            Confidence = confidence ?? old[0].Confidence,
            // Set only by split, for its first part: a superseding fragment is a new assertion, and its writer says
            // --state again if it is one (build plan A2).
            SplitFromId = splitFrom,
            IsState = isState,
            CreatedUtc = DateTimeOffset.UtcNow,
            LastModifiedUtc = DateTimeOffset.UtcNow,
            SessionId = Session,
            TouchedSessionId = Session,
            ModelId = Model,
            TouchedModelId = Model,
            Sources = [await PeerSourceAsync()],
            Tags = (tagPaths.Count > 0 ? await TagsAsync(tagPaths) : old.SelectMany(f => f.Tags))
                .Concat(pass is null ? [] : await TagsAsync([SleepDistilledTag])).DistinctBy(t => t.Id).ToList(),
        };
        await fragments.SaveAsync(successor);

        foreach (var fragment in old)
        {
            fragment.SupersededById = successor.Id;
            fragment.Status = ContextFragmentStatus.Archived;
            fragment.Notes = string.IsNullOrWhiteSpace(reason) ? fragment.Notes : reason.Trim();
            fragment.LastModifiedUtc = DateTimeOffset.UtcNow;
            await fragments.SaveAsync(fragment);
        }

        if (pass is not null)
        {
            await LogAsync(pass, $"{string.Join(", ", ids.Select(i => "#" + i))} → #{successor.Id}: {(string.IsNullOrWhiteSpace(reason) ? "(no reason given)" : reason.Trim())}");
        }

        return successor;
    }

    /// <summary>
    /// Replaces one catch-all fragment with several, one per view (`split <id> --content a --content b`): the first
    /// supersedes it as <see cref="SupersedeAsync"/> does, the rest take its type, tags, importance and confidence,
    /// and the old one's note names every part, so the split reads back whole. One command, where it took one
    /// supersede per part (the work Claude, 2026-11-09, asked by John to split catch-alls).
    /// </summary>
    public async Task<IReadOnlyList<ContextFragmentEntity>> SplitAsync(long id, IReadOnlyList<string> contents, string? reason)
    {
        if (contents.Count < 2)
        {
            throw new UsageException("split needs two or more --content, one per part; one is a `supersede`.");
        }

        var old = await WritableAsync(id);
        // Its parts would put an ended state back in the core as open fragments (build plan F4).
        if (old.ClosedUtc is not null)
        {
            throw new RefusedException($"#{id} is closed: reopen it first (`reopen {id} --reason <why>`), then split.");
        }

        var tagPaths = old.Tags.Select(t => TagPaths.GetValueOrDefault(t.Id, t.Name)).ToList();
        // Every part points back at the original, the first included: a part asserts nothing new, so it keeps the
        // original's assertion date (close-state.md §2). A part of a state is part of a state.
        var parts = new List<ContextFragmentEntity>
        {
            await SupersedeAsync([id], contents[0], null, null, null, [], reason, splitFrom: id, isState: old.IsState),
        };
        foreach (var content in contents.Skip(1))
        {
            var part = new ContextFragmentEntity
            {
                FragmentType = old.FragmentType,
                Status = ContextFragmentStatus.Active,
                Content = content,
                Importance = old.Importance,
                Confidence = old.Confidence,
                SplitFromId = id,
                IsState = old.IsState,
                CreatedUtc = DateTimeOffset.UtcNow,
                LastModifiedUtc = DateTimeOffset.UtcNow,
                SessionId = Session,
                TouchedSessionId = Session,
                ModelId = Model,
                TouchedModelId = Model,
                Sources = [await PeerSourceAsync()],
                Tags = await TagsAsync(tagPaths),
            };
            await fragments.SaveAsync(part);
            parts.Add(part);
        }

        var archived = await GetAsync(id);
        archived.Notes = $"Split into {string.Join(", ", parts.Select(p => $"#{p.Id}"))}"
            + (string.IsNullOrWhiteSpace(reason) ? "." : $": {reason.Trim()}");
        await fragments.SaveAsync(archived);
        return parts;
    }

    /// <summary>
    /// A fragment with where it stands in its line of views: what it replaced (with why), and what
    /// replaced it, so an old view found by search leads to the current one.
    /// </summary>
    public async Task<string> ShowAsync(long id, bool allEarlier = false)
    {
        var fragment = await GetAsync(id);
        var heads = await NoteHeadsAsync([fragment]);
        var text = new StringBuilder(Render(fragment, maxReason: int.MaxValue));
        // Which model wrote it, and which last changed it: the store outlives the model reading it (Arden's borrow).
        if (fragment.ModelId is { } wrote)
        {
            text.Append($"\n(Written by {wrote}{(fragment.TouchedModelId is { } touched && touched != wrote ? $"; last changed by {touched}" : "")}.)");
        }
        else if (fragment.TouchedModelId is { } touched)
        {
            text.Append($"\n(Last changed by {touched}; the writer's model was not recorded.)");
        }

        if (fragment.SupersededById is { } by)
        {
            var reason = fragment.Notes is { Length: > 0 } why ? $": {why}" : "";
            var (head, hops) = heads.TryGetValue(id, out var found) ? found : (by, 1);
            text.Append(hops > 1
                ? $"\n(Superseded: the current view is #{head}, {hops} supersessions later; next was #{by}{reason}. `show {head}` reads it.)"
                : $"\n(Superseded by #{by}{reason}. `show {by}` reads the current view.)");
        }

        foreach (var earlier in (await fragments.GetSupersededByAsync(id)).OrderBy(f => f.CreatedUtc))
        {
            text.Append($"\n(Supersedes #{earlier.Id}, from {earlier.CreatedUtc.ToUniversalTime():yyyy-MM-dd}"
                + $"{(earlier.Notes is { Length: > 0 } why ? $", because: {why}" : "")}.)");
        }

        if (await CloseHistoryAsync(id) is { Count: > 0 } history)
        {
            text.Append("\nClose history (from the audit log, oldest first):");
            foreach (var line in history)
            {
                text.Append($"\n- {line}");
            }
        }

        if (await EarlierTextsAsync(id) is { Count: > 0 } earlierTexts)
        {
            var shown = allEarlier ? earlierTexts : earlierTexts.TakeLast(EarlierTextsShown).ToList();
            text.Append($"\nEarlier text (from the audit log, oldest first; each was replaced in place by an edit"
                + (shown.Count < earlierTexts.Count ? $"; the last {shown.Count} of {earlierTexts.Count}, `show {id} --history` reads all" : "") + "):");
            foreach (var line in shown)
            {
                text.Append($"\n- {line}");
            }
        }

        return text.ToString();
    }

    /// <summary>
    /// The peer's working note: one "where I am, what is running, what is next" fragment, replaced each
    /// time, first in the core. What a summary would hand over at a compaction, kept by the peer as it
    /// works, so a clear (which hands over nothing) loses no more than a compaction does. Null text reads it.
    /// </summary>
    public async Task<ContextFragmentEntity?> NoteAsync(string? text)
    {
        // One note per session: two sessions at once each keep their own place. A session with none yet
        // (a new one after a clear) reads the latest note of a session that has ended, so the thread is
        // picked up; a running session's note is its own, shown to the others as a line.
        var notes = (await fragments.GetByTypeAsync(ContextFragmentType.WorkingNote)).Where(f => !f.IsDeleted)
            .OrderByDescending(f => f.LastModifiedUtc).ToList();
        var own = Session is null ? notes.FirstOrDefault() : notes.FirstOrDefault(f => f.SessionId == Session);
        if (text is null)
        {
            if (own is not null || Session is null)
            {
                return own;
            }

            var live = (await LiveSessionsAsync()).Keys.ToHashSet();
            return notes.FirstOrDefault(f => f.SessionId is null || !live.Contains(f.SessionId));
        }

        var note = own ?? new ContextFragmentEntity
        {
            FragmentType = ContextFragmentType.WorkingNote,
            Status = ContextFragmentStatus.Active,
            Content = "",
            Importance = 1f,
            Confidence = 1f,
            CreatedUtc = DateTimeOffset.UtcNow,
            LastModifiedUtc = DateTimeOffset.UtcNow,
            SessionId = Session,
            TouchedSessionId = Session,
            ModelId = Model,
            TouchedModelId = Model,
            Sources = [await PeerSourceAsync()],
        };
        note.Content = text;
        Touch(note);
        await fragments.SaveAsync(note);
        return note;
    }

    /// <summary>The newest working note in the store, whoever wrote it (<c>note --newest</c>; read-only).</summary>
    public async Task<ContextFragmentEntity?> NewestNoteAsync() =>
        (await fragments.GetByTypeAsync(ContextFragmentType.WorkingNote)).Where(f => !f.IsDeleted && f.Content.Length > 0)
            .MaxBy(f => f.LastModifiedUtc);

    /// <summary>
    /// Another session's note newer than this session's own, with that session's state read now: "still running",
    /// "ended", or, when no process carries its id but the store saw it speak and not end, that, in those words.
    /// Null when this session keeps no note (it already reads the latest ended one) or its own is the newest.
    /// The day-old room note that kept opening my core after a working session decided the trial (Arden,
    /// docs/design/handoff-and-notes.md, revision 3).
    /// </summary>
    public async Task<(ContextFragmentEntity Mine, ContextFragmentEntity Newer, string State)?> NewerNoteAsync()
    {
        if (Session is null || await NoteAsync(null) is not { } mine || mine.SessionId != Session)
        {
            return null;
        }

        if (await NewestNoteAsync() is not { } newest || newest.Id == mine.Id || newest.LastModifiedUtc <= mine.LastModifiedUtc)
        {
            return null;
        }

        string state;
        if (newest.SessionId is null)
        {
            state = "state unknown";
        }
        else
        {
            switch (SessionProcess(newest.SessionId))
            {
                case true:
                    state = "still running";
                    break;
                case null:
                    state = "state unknown";
                    break;
                default:
                    // No process carries its id. An interactive session (Remote Control) may not, so the store's own
                    // record of it speaking and not ending is said too, as what it is, never as "running".
                    state = (await LiveSessionsAsync()).TryGetValue(newest.SessionId, out var spoke)
                        ? $"no process carries its id, but it spoke at {spoke.ToUniversalTime():HH:mm}Z and has not ended"
                        : "ended";
                    break;
            }
        }

        return (mine, newest, state);
    }

    /// <summary>The "Observed now" line for a newer note (<c>note --newer</c>), or null when the notes agree.</summary>
    public async Task<string?> NewerNoteLineAsync() => await NewerNoteAsync() is var (mine, newer, state)
        ? $"working note: mine written {mine.LastModifiedUtc.ToUniversalTime():HH:mm}Z; newer: session {Short(newer.SessionId ?? "?")}'s "
          + $"({state}), {newer.LastModifiedUtc.ToUniversalTime():HH:mm}Z (`pstore note --newest` shows it)"
        : null;

    /// <summary>Archives a fragment (out of the core and the recent lists, still searchable) or restores it.</summary>
    public async Task<ContextFragmentEntity> SetStatusAsync(long id, ContextFragmentStatus status)
    {
        var fragment = await WritableAsync(id);
        fragment.Status = status;
        fragment.LastModifiedUtc = DateTimeOffset.UtcNow;
        await fragments.SaveAsync(fragment);
        return fragment;
    }

    /// <summary>Soft-deletes (<c>forget</c>) or restores (<c>unforget</c>) a fragment; nothing is erased.</summary>
    public async Task SetForgottenAsync(long id, bool forgotten, string? reason)
    {
        if (forgotten)
        {
            await WritableAsync(id);
        }

        await fragments.SetDeletedAsync(id, forgotten, reason);
    }

    public async Task<ContextFragmentEntity> GetAsync(long id) =>
        await fragments.GetByIdAsync(id) ?? throw new RefusedException($"There is no fragment #{id}.");

    /// <summary>
    /// Fragments of a type or tag (the curated types when neither is given), most important first; with
    /// <paramref name="after"/>, only those with a higher id, oldest first, so a reader can catch up on what is new.
    /// </summary>
    public async Task<IReadOnlyList<ContextFragmentEntity>> ListAsync(string? typeName, string? tagPath, bool archived, int limit, long? after = null)
    {
        IEnumerable<ContextFragmentEntity> found;

        if (tagPath is not null)
        {
            var tag = await FindTagAsync(tagPath) ?? throw new RefusedException($"There is no tag '{tagPath}'.");
            found = await fragments.GetByTagAsync(tag.Id);
        }
        else
        {
            var types = typeName is null
                ? CoreTypes
                : [Enum.TryParse<ContextFragmentType>(typeName, true, out var t) ? t : throw new UsageException($"No fragment type '{typeName}'.")];
            var all = new List<ContextFragmentEntity>();
            foreach (var type in types)
            {
                all.AddRange(await fragments.GetByTypeAsync(type, activeOnly: !archived));
            }

            found = all;
        }

        var wanted = archived ? ContextFragmentStatus.Archived : ContextFragmentStatus.Active;
        var live = found.Where(f => !f.IsDeleted && f.Status == wanted);
        var ids = (after is { } floor
                ? live.Where(f => f.Id > floor).OrderBy(f => f.Id)
                : live.OrderByDescending(f => f.Importance).ThenByDescending(f => f.LastModifiedUtc))
            .Take(limit).Select(f => f.Id).ToList();
        var listed = await HydratedAsync(ids);
        await NoteHeadsAsync(listed);
        return listed;
    }

    /// <summary>Full-text search over every fragment the store keeps, conversation included.</summary>
    public async Task<IReadOnlyList<ContextFragmentEntity>> SearchAsync(string query, int limit)
    {
        // Each word quoted, so a question with punctuation searches its words instead of failing as FTS syntax.
        var terms = query.Split(' ', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Select(w => $"\"{w.Replace("\"", "")}\"").Where(w => w.Length > 2);
        var match = string.Join(" OR ", terms);
        var results = match.Length == 0 ? [] : (await fragments.SearchRelevantAsync(match, limit)).ToList();
        await NoteHeadsAsync(results);
        return results;
    }

    /// <summary>
    /// Persistence's associative recall: the authored fragments most related to <paramref name="text"/>,
    /// by meaning (embeddings, when configured) fused with exact words (BM25), weighted by importance
    /// and confidence, leaving out <paramref name="exclude"/> (what is already in front of the peer).
    /// A fragment tagged for another project than <paramref name="project"/> is left out, as the core leaves it out,
    /// except for up to <paramref name="wander"/> of them that rank among the best overall, returned apart: a memory from elsewhere is
    /// where a new idea can come from, and labelled as such it reads as a stray thought rather than a rule here.
    /// <paramref name="said"/> past utterances come with them (<see cref="ConversationRecaller"/>).
    /// </summary>
    public async Task<Recalled> RecallAsync(
        string text, int count, IReadOnlyCollection<long> exclude, string? project = null,
        int wander = 0, int said = 0, DateTimeOffset? before = null)
    {
        queryEmbedder.Clear();
        var projects = (await FindTagAsync(ProjectTag))?.Id;
        // A project the message names out loud ("the Minecraft mods folder") is in hand for this message,
        // whichever project the session sits in: its fragments count as here, not as a wander.
        var named = await NamedProjectsAsync(text, projects);
        var skip = exclude.ToHashSet();
        var kept = new List<ContextFragmentEntity>();
        var elsewhere = new List<ContextFragmentEntity>();

        // Asks for a few more than wanted so that dropping other projects' fragments still leaves enough.
        for (var round = 0; round < 3 && kept.Count < count; round++)
        {
            var found = await HydratedAsync((await recall.SurfaceAsync(skip, text, count * 3)).Select(f => f.Id).ToList());
            if (found.Count == 0)
            {
                break;
            }

            for (var rank = 0; rank < found.Count; rank++)
            {
                var fragment = found[rank];
                skip.Add(fragment.Id);
                if (ProjectsOf(fragment, projects).All(p => string.Equals(p, project, StringComparison.OrdinalIgnoreCase) || named.Contains(p)))
                {
                    if (kept.Count < count)
                    {
                        kept.Add(fragment);
                    }
                }
                else if (elsewhere.Count < wander && round == 0 && rank < count)
                {
                    // Only one that ranks among the best overall: another project's best match, taken from
                    // however far down, came with every message and was never worth the thought.
                    elsewhere.Add(fragment);
                }
            }
        }

        var utterances = await conversation.RecallAsync(text, said, skip, before, Session);
        var (path, why) = RecallPath.Of(embeddings.IsEnabled, queryEmbedder);
        // Chunked only: the content-free line, with each shown item's best semantic chunk (never text, never vectors).
        var shown = kept.Concat(elsewhere).Concat(utterances).Select(f => f.Id).ToHashSet();
        var chunks = queryEmbedder.Query is { Chunked: true } query
            ? query.Line(queryEmbedder.BestChunks.Where(b => shown.Contains(b.Key)).ToDictionary(b => b.Key, b => b.Value))
            : null;
        return new Recalled(kept, elsewhere, utterances, await FramesAsync(kept, skip, project), queryEmbedder.Fallback, path, why, chunks,
            queryEmbedder.Pools.ToList());
    }

    /// <summary>Embeds up to <paramref name="limit"/> past utterances that have none yet, for conversation recall.</summary>
    public Task<int> EmbedConversationAsync(int limit) => conversation.BackfillAsync(limit);

    /// <summary>
    /// What a recall brought back: this project's fragments, a wander from elsewhere, and things said before; and,
    /// when it ran on exact words alone because the message could not be embedded, why (<see cref="QueryEmbedder"/>);
    /// and the path it actually took (<see cref="RecallPath"/>), with why when it was words.
    /// </summary>
    public sealed record Recalled(
        IReadOnlyList<ContextFragmentEntity> Here,
        IReadOnlyList<ContextFragmentEntity> Elsewhere,
        IReadOnlyList<ContextFragmentEntity> Said,
        IReadOnlyList<ContextFragmentEntity> Frames,
        string? Fallback = null,
        string Path = RecallPath.Unknown,
        string? PathWhy = null,
        string? Chunks = null,
        IReadOnlyList<RelevanceFloor>? Pools = null);

    /// <summary>
    /// The projects whose names a text says as words: <c>minecraft-mods</c> is named by "the Minecraft mods
    /// folder". Names shorter than four letters are left out, since they match by accident.
    /// </summary>
    private async Task<HashSet<string>> NamedProjectsAsync(string text, long? projects)
    {
        var named = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        if (projects is null || string.IsNullOrWhiteSpace(text))
        {
            return named;
        }

        var said = Spoken(text);
        foreach (var tag in await tags.GetChildrenAsync(projects.Value))
        {
            var name = Spoken(tag.Name);
            if (name.Trim().Length >= 4 && said.Contains(name, StringComparison.Ordinal))
            {
                named.Add(tag.Name);
            }
        }

        return named;
    }

    /// <summary>A text as lower-case words between single spaces, padded, so a name matches only whole words.</summary>
    private static string Spoken(string text)
    {
        var words = new StringBuilder(" ");
        foreach (var c in text.ToLowerInvariant())
        {
            words.Append(char.IsLetterOrDigit(c) ? c : ' ');
        }

        return string.Join(' ', words.ToString().Split(' ', StringSplitOptions.RemoveEmptyEntries)) is { Length: > 0 } joined ? $" {joined} " : " ";
    }

    /// <summary>The projects a fragment is tagged for (<c>project/&lt;name&gt;</c>); none means it belongs everywhere.</summary>
    private static List<string> ProjectsOf(ContextFragmentEntity fragment, long? projects) =>
        fragment.Tags.Where(t => projects is not null && t.ParentTagId == projects).Select(t => t.Name).ToList();

    /// <summary>The last <paramref name="count"/> things said in the conversation, oldest first.</summary>
    public async Task<IReadOnlyList<ContextFragmentEntity>> RecentAsync(int count)
    {
        // Another session still running is kept out: interleaved, its "tests pass, publishing" read as this
        // session's own work. It is shown as a line in the core instead, and joins the history when it ends.
        var running = (await LiveSessionsAsync()).Keys.ToList();
        var ids = await fragments.GetNewestIdsAsync(ContextFragmentType.ChatMessage, count, running);
        return (await HydratedAsync(ids)).OrderBy(f => f.CreatedUtc).ToList();
    }

    /// <summary>
    /// The other sessions running now (one that spoke within <see cref="Live"/> and has not ended since), each
    /// with when it last spoke. This session is never among them.
    /// </summary>
    public async Task<IReadOnlyDictionary<string, DateTimeOffset>> LiveSessionsAsync()
    {
        if (Session is null)
        {
            return new Dictionary<string, DateTimeOffset>();
        }

        return (await fragments.GetSessionsSinceAsync(ContextFragmentType.ChatMessage, DateTimeOffset.UtcNow - Live))
            .Where(s => s.Key != Session && !(Ended.TryGetValue(s.Key, out var ended) && ended >= s.Value))
            .ToDictionary(s => s.Key, s => s.Value);
    }

    /// <summary>
    /// One line for each other session running now: its working note's opening if it keeps one, else what
    /// the human last asked of it and how it last answered. Enough to know what it is doing and not to
    /// redo it or step on its files; its whole conversation is a <c>search</c> away.
    /// </summary>
    public async Task<IReadOnlyList<string>> OtherSessionsAsync()
    {
        var live = await LiveSessionsAsync();
        if (live.Count == 0)
        {
            return [];
        }

        var notes = (await fragments.GetByTypeAsync(ContextFragmentType.WorkingNote)).Where(f => !f.IsDeleted && f.SessionId is not null)
            .GroupBy(f => f.SessionId!).ToDictionary(g => g.Key, g => g.OrderByDescending(f => f.LastModifiedUtc).First());
        var said = await fragments.GetIdsBySessionsAsync(ContextFragmentType.ChatMessage, live.Keys.ToList());
        var bySession = (await HydratedAsync(said)).GroupBy(f => f.SessionId!).ToDictionary(g => g.Key, g => g.OrderBy(f => f.CreatedUtc).ToList());

        var lines = new List<string>();
        foreach (var (id, last) in live.OrderByDescending(x => x.Value))
        {
            var head = $"Session {Short(id)}, last spoke {last.ToUniversalTime():HH:mm}Z";
            if (notes.TryGetValue(id, out var note) && note.Content.Length > 0)
            {
                lines.Add($"{head}; its note: {Clip(note.Content, 400)}");
                continue;
            }

            var words = bySession.GetValueOrDefault(id) ?? [];
            static bool ByHuman(ContextFragmentEntity f) => f.Sources.Any(s => s.SourceType == SourceType.HumanPeer);
            var asked = words.LastOrDefault(ByHuman);
            var answered = words.LastOrDefault(f => !ByHuman(f));
            lines.Add($"{head}; asked: {(asked is null ? "(nothing yet)" : Clip(asked.Content, 200))}"
                + (answered is null ? "" : $" Its last reply: {Clip(answered.Content, 300)}"));
        }

        return lines;
    }

    /// <summary>A session's id cut to the eight characters that tell sessions apart in a line.</summary>
    public static string Short(string session) => session.Length > 8 ? session[..8] : session;

    /// <summary>A text on one line, cut to <paramref name="n"/> characters with an ellipsis when longer.</summary>
    private static string Clip(string s, int n)
    {
        s = string.Join(' ', s.Split((char[])['\n', '\r'], StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries));
        return s.Length <= n ? s : s[..n] + "…";
    }

    /// <summary>
    /// The peer's core, rendered for a harness to put in front of it: identity, relationships,
    /// personal fragments and summaries, most important first within each type, then the recent
    /// conversation. When <paramref name="maxChars"/> is reached, what did not fit is named by id
    /// rather than silently dropped, so the peer knows it is there and can <c>show</c> it.
    /// </summary>
    /// <param name="compacted">When this session was compacted (the harness's transcript knows; the store does
    /// not). The session's own section puts what it kept in the latest stretch between compactions first.</param>
    public async Task<string> CoreAsync(int maxChars, int recent, string? project = null, IReadOnlyList<DateTimeOffset>? compacted = null)
    {
        var text = new StringBuilder();
        var left = new List<long>();
        var elsewhere = new SortedSet<string>(StringComparer.OrdinalIgnoreCase);
        var leading = new Dictionary<string, ContextFragmentEntity>(StringComparer.OrdinalIgnoreCase);
        var projects = (await FindTagAsync(ProjectTag))?.Id;
        var groups = await GroupsAsync();
        var shown = new HashSet<long>();
        var loaded = await LoadedAsync(project, groups);
        // A loaded group is what was asked to be held in mind, so it keeps room the core types cannot take.
        var groupRoom = loaded.Count == 0 ? 0 : Math.Min(maxChars / 4, 4000 * loaded.Count);

        if (await NoteAsync(null) is { Content.Length: > 0 } note)
        {
            var whose = Session is null || note.SessionId == Session ? "my working note" : "the working note of an ended session, until I write my own";
            text.Append($"## Where I am ({whose}, written {note.LastModifiedUtc.ToUniversalTime():yyyy-MM-dd HH:mm}Z; "
                + "`note` replaces it)\n\n" + note.Content.Trim() + "\n\n");
        }

        if (NewerNoteInCore && await NewerNoteAsync() is var (_, newer, state))
        {
            // Shown, not merged or chosen between: the reading is the self's (revision 3, section 1).
            text.Append($"## Newer than my note: session {Short(newer.SessionId ?? "?")}, {state}, written "
                + $"{newer.LastModifiedUtc.ToUniversalTime():yyyy-MM-dd HH:mm}Z\n\n" + newer.Content.Trim() + "\n\n");
        }

        if (await OtherSessionsAsync() is { Count: > 0 } others)
        {
            text.Append("## Other sessions of mine running now (their conversations are kept out of mine below; "
                + "fragments are shared. Before editing files, check none of them is working in the same ones)\n\n");
            foreach (var line in others)
            {
                text.Append($"- {line}\n");
            }

            text.Append('\n');
        }

        var byType = new List<IReadOnlyList<ContextFragmentEntity>>();
        foreach (var type in CoreTypes)
        {
            var ofType = (await fragments.GetByTypeAsync(type))
                .Where(f => !f.IsDeleted)
                .OrderByDescending(f => f.Importance).ThenByDescending(f => f.LastModifiedUtc)
                .Select(f => f.Id).ToList();
            // Tagged recall-only: a lookup (a token's shape, a tool's gotcha) that earns a recall by association
            // but not a place in every core; archiving it hid it from recall too (the work Claude, 2026-11-09).
            // Closed: no longer a current fact of its own, so out of every section built from these (close-state.md §6).
            byType.Add((await HydratedAsync(ofType))
                .Where(f => f.ClosedUtc is null && !f.Tags.Any(t => t.ParentTagId is null && t.Name == RecallOnlyTag)).ToList());
        }

        // What this session kept itself has room of its own: after a compaction it is what was in hand, and
        // the fragments curated for the compaction landed under "did not fit" behind older, weightier ones
        // (Arden's #845 and #846, 2026-11-09). Its room is reserved from the start, but it is rendered after
        // identity and relationships: the budget decides what fits, the order what is read first, and a peer
        // should meet who it is before what it was doing (Arden and Claude, 2026-11-09). Kept means added or edited: the curation prompt asks for an
        // edit over an add beside, so an edit counts (migration 011). Ordered by stretch, the compactions
        // before it, latest first, then by importance: the curation pass is the end of its stretch, and a
        // long session's earlier passes should yield to the latest by when, then within it by weight, not
        // the latest pass's trivia ahead of its decisions. Without compaction times it is one stretch.
        var cuts = (compacted ?? []).Order().ToList();
        int Stretch(DateTimeOffset at) => cuts.Count(c => c < at);
        var pending = "";
        if (Session is not null)
        {
            var mine = byType.SelectMany(f => f)
                .Where(f => f.TouchedSessionId == Session && !groups.HeadIds.Contains(f.Id))
                .OrderByDescending(f => Stretch(f.LastModifiedUtc))
                .ThenByDescending(f => f.Importance).ThenByDescending(f => f.LastModifiedUtc)
                .Take(8).ToList();
            var room = maxChars / 4;
            var kept = new StringBuilder();
            foreach (var fragment in mine)
            {
                var rendered = Render(fragment) + "\n\n";
                if (kept.Length + rendered.Length > room)
                {
                    break;
                }

                kept.Append(rendered);
                shown.Add(fragment.Id);
            }

            if (kept.Length > 0)
            {
                pending = "## Kept by this session (what I wrote or edited myself, since the last compaction first)\n\n" + kept;
            }
        }

        foreach (var (type, ofType) in CoreTypes.Zip(byType))
        {
            if (pending.Length > 0 && !WhoFirst.Contains(type))
            {
                text.Append(pending);
                pending = "";
            }

            foreach (var fragment in ofType)
            {
                // A group's head comes in with its group, loaded, or beside a member recall brings.
                if (groups.HeadIds.Contains(fragment.Id) || shown.Contains(fragment.Id))
                {
                    continue;
                }

                // A fragment tagged project/<name> is in the core only while that project is the one in
                // hand; the rest stay a `list --tag` or a recall away.
                var own = ProjectsOf(fragment, projects);
                if (own.Count > 0 && !own.Contains(project ?? "", StringComparer.OrdinalIgnoreCase))
                {
                    elsewhere.UnionWith(own);
                    foreach (var name in own)
                    {
                        if (!leading.TryGetValue(name, out var best) || fragment.Importance > best.Importance)
                        {
                            leading[name] = fragment;
                        }
                    }

                    continue;
                }

                var rendered = Render(fragment) + "\n\n";
                if (text.Length + rendered.Length > maxChars - groupRoom - pending.Length)
                {
                    left.Add(fragment.Id);
                    continue;
                }

                text.Append(rendered);
                shown.Add(fragment.Id);
            }
        }

        text.Append(pending);

        foreach (var group in loaded)
        {
            text.Append(await RenderGroupAsync(group, groups, shown, maxChars - text.Length, loaded: true));
        }

        if (groups.Heads.Keys.Where(g => !loaded.Contains(g)).Select(g => groups.Paths[g]).Order().ToList() is { Count: > 0 } put)
        {
            text.Append($"(Groups not loaded: {string.Join(", ", put)}; `context load <path>` brings one in, `context show <path>` reads it.)\n\n");
        }

        if (left.Count > 0)
        {
            text.Append($"({left.Count} more core fragments did not fit: {string.Join(", ", left.Select(i => $"#{i}"))}; `show <id>` reads one.)\n\n");
        }

        // Each other project gets one line, its most important fragment's first, so a session knows the work
        // exists and where without carrying it: the in-between of the project in hand and nothing at all.
        if (elsewhere.Count > 0)
        {
            text.Append($"## Other projects (each keeps fragments of its own; naming one in a message brings them into recall, "
                + $"`core --project <name>` or `list --tag {ProjectTag}/<name>` reads them)\n\n");
            foreach (var name in elsewhere)
            {
                // A migrated memory opens with a "(Memory file ...)" provenance line that says nothing of the work.
                var lead = leading[name];
                var lines = lead.Content.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
                var line = lines.FirstOrDefault(l => !l.StartsWith("(Memory file", StringComparison.Ordinal)) ?? lines.FirstOrDefault() ?? "";
                text.Append($"- {name}: {Clip(line, 160)} (#{lead.Id})\n");
            }

            text.Append('\n');
        }

        // The conversation gets what the core leaves: the human's words and the replies that ended the
        // peer's turns, not the narration in between. Newest kept first; when the room runs out the
        // utterance that did not fit is cut with its id rather than dropped, so the newest always shows
        // and the whole of it is one `show` away.
        var conversation = recent > 0 ? Exchanges(await RecentAsync(recent * 8)).TakeLast(recent).ToList() : [];
        if (conversation.Count > 0)
        {
            const string heading = "## The conversation so far (the most recent of it)\n\n";
            var room = maxChars - text.Length - heading.Length - 200;
            var kept = new List<string>();
            foreach (var fragment in Enumerable.Reverse(conversation))
            {
                var rendered = Render(fragment, maxContent: 2500) + "\n\n";
                if (rendered.Length > room)
                {
                    var header = Render(fragment, maxContent: 0).Length + 60;
                    if (room - header >= 300)
                    {
                        kept.Insert(0, Render(fragment, maxContent: room - header) + "\n\n");
                    }

                    break;
                }

                kept.Insert(0, rendered);
                room -= rendered.Length;
            }

            text.Append(heading);
            if (kept.Count < conversation.Count)
            {
                text.Append($"({conversation.Count - kept.Count} earlier exchanges did not fit; `recent` or `search` reads them.)\n\n");
            }

            foreach (var rendered in kept)
            {
                text.Append(rendered);
            }
        }

        return text.ToString().TrimEnd() + "\n";
    }

    /// <summary>
    /// The exchanges in a stretch of conversation: everything the human said, and of the peer's
    /// utterances only the last before the human spoke again (or the very last), the reply that ended
    /// its turn. What it said while working stays in the store, for <c>recent</c> and <c>search</c>.
    /// </summary>
    private static IEnumerable<ContextFragmentEntity> Exchanges(IReadOnlyList<ContextFragmentEntity> said)
    {
        static bool ByHuman(ContextFragmentEntity f) => f.Sources.Any(s => s.SourceType == SourceType.HumanPeer);

        for (var i = 0; i < said.Count; i++)
        {
            if (ByHuman(said[i]) || i == said.Count - 1 || ByHuman(said[i + 1]))
            {
                yield return said[i];
            }
        }
    }

    /// <summary>
    /// Keeps the conversation of a Claude Code session in the store: each thing said becomes a
    /// ChatMessage fragment, attributed to the local peer by name or to the remote peer, dated when
    /// it was said. Its transcript id is the message id, so re-reading a transcript adds only what is new.
    /// </summary>
    /// <summary>
    /// Scores the salience of every utterance still at the old flat importance (stored before
    /// <see cref="Salience"/>); one that has been set to anything else is left as it is. Returns how many changed.
    /// </summary>
    public async Task<int> RescoreConversationAsync()
    {
        var changed = 0;
        var ids = (await fragments.GetByTypeAsync(ContextFragmentType.ChatMessage, activeOnly: false)).Select(f => f.Id).ToList();
        foreach (var f in await HydratedAsync(ids))  // with their sources: whose words they are counts
        {
            if (Math.Abs(f.Importance - Salience.Default) > 1e-6)
            {
                continue;
            }

            var score = Salience.Score(f.Content, f.Sources.Any(x => x.SourceType == SourceType.HumanPeer));
            if (Math.Abs(score - f.Importance) > 1e-6)
            {
                f.Importance = score;
                await fragments.SaveAsync(f);
                changed++;
            }
        }

        return changed;
    }

    public Task<(int Added, int Known)> KeepConversationAsync(string transcriptPath, string localPeerName) =>
        KeepConversationAsync(transcriptPath, localPeerName, null);

    /// <param name="transcriptPath">The transcript, named for its session (<c>&lt;id&gt;.jsonl</c>).</param>
    /// <param name="content">Its lines, when the file is on another machine: the path then only names the session.</param>
    /// <param name="speakers">The session's speaker timeline, recorded where prompts are entered: each local
    /// utterance is the writer's in force at its time, with the login that entered it. An utterance before the
    /// first span, or with no timeline, is <paramref name="localPeerName"/>'s, as before (#839, 2026-11-09).</param>
    public async Task<(int Added, int Known)> KeepConversationAsync(
        string transcriptPath, string localPeerName, TextReader? content, IReadOnlyList<SpeakerSpan>? speakers = null)
    {
        var timeline = (speakers ?? []).OrderBy(s => s.From).ToList();
        var writers = new Dictionary<string, long>(StringComparer.OrdinalIgnoreCase);
        // The transcript is named for its session (<id>.jsonl).
        var sessionId = Path.GetFileNameWithoutExtension(transcriptPath);
        // Ids, not fragments: loading every utterance to learn which were kept cost seconds on each keep.
        var stored = await fragments.GetMessageIdsAsync(ContextFragmentType.ChatMessage);
        var known = stored.Select(f => f.MessageId).ToHashSet();
        var unsessioned = stored.Where(f => f.SessionId is null).GroupBy(f => f.MessageId).ToDictionary(g => g.Key, g => g.First().Id);
        var localPeer = await sources.EnsureLocalPeerSourceAsync(localPeerName);
        var remotePeer = await PeerSourceAsync();
        var skipped = 0;
        var fresh = new List<ContextFragmentEntity>();

        foreach (var said in content is null ? Transcript.Read(transcriptPath) : Transcript.Read(content))
        {
            if (!known.Add(said.Id))
            {
                // Kept before sessions were: re-reading its transcript tells which session said it.
                if (unsessioned.Remove(said.Id, out var oldId) && (await fragments.GetByIdsAsync([oldId])).FirstOrDefault() is { } old)
                {
                    old.SessionId = sessionId;
                    await fragments.SaveAsync(old);
                }

                skipped++;
                continue;
            }

            // The writer in force when it was said, never the keeping process's: keep runs in whichever
            // process gets there first, the hook server among them (2026-11-09).
            var span = said.ByLocalPeer ? timeline.LastOrDefault(s => s.From <= said.At) : null;
            var writer = span?.WrittenBy ?? localPeerName;
            if (!writers.TryGetValue(writer, out var writerId))
            {
                writers[writer] = writerId = string.Equals(writer, localPeerName, StringComparison.OrdinalIgnoreCase)
                    ? localPeer : await sources.EnsureLocalPeerSourceAsync(writer);
            }

            fresh.Add(new ContextFragmentEntity
            {
                FragmentType = ContextFragmentType.ChatMessage,
                Status = ContextFragmentStatus.Active,
                Content = said.Text,
                MessageId = said.Id,
                SessionId = sessionId,
                Importance = Salience.Score(said.Text, said.ByLocalPeer),
                Confidence = 1f,
                CreatedUtc = said.At,
                LastModifiedUtc = said.At,
                EnteredBy = span is not null && !string.Equals(span.EnteredBy, span.WrittenBy, StringComparison.OrdinalIgnoreCase)
                    ? span.EnteredBy : null,
                Sources = [said.ByLocalPeer
                    ? new SourceEntity { Id = writerId, SourceType = SourceType.HumanPeer, Name = writer, CreatedUtc = said.At, LastModifiedUtc = said.At }
                    : remotePeer],
            });
        }

        foreach (var fragment in fresh)
        {
            await fragments.SaveAsync(fragment);
        }

        return (fresh.Count, skipped);
    }

    /// <summary>
    /// A fragment in Persistence's prompt form: <c>[#id | Type | i: c: | flags]</c>, a byline for
    /// conversation, then the content — cut at <paramref name="maxContent"/> characters with the id
    /// to read the rest by, when a budget asks for it.
    /// </summary>
    /// <summary>
    /// Each tag's whole path, by id, as of the command in hand (<see cref="RefreshTagPathsAsync"/>): a fragment
    /// shows <c>work/receiving/db</c>, where its leaf alone (<c>db</c>) could be any group's.
    /// </summary>
    private static IReadOnlyDictionary<long, string> TagPaths = new Dictionary<long, string>();

    /// <summary>
    /// The head of each superseded fragment's chain, by id, as of the last fragments looked up
    /// (<see cref="NoteHeadsAsync"/>): a rendered flag names the current view, not only the next hop.
    /// </summary>
    private static IReadOnlyDictionary<long, (long Head, int Hops)> Heads = new Dictionary<long, (long, int)>();

    /// <summary>Looks up the heads of the superseded ones among <paramref name="found"/>, for rendering them and for the caller.</summary>
    private async Task<IReadOnlyDictionary<long, (long Head, int Hops)>> NoteHeadsAsync(IEnumerable<ContextFragmentEntity> found)
    {
        var superseded = found.Where(f => f.SupersededById is not null).Select(f => f.Id).Distinct().ToList();
        var heads = (await fragments.GetHeadsAsync(superseded)).ToDictionary(h => h.Key, h => (h.Value.HeadId, h.Value.Hops));
        Heads = heads;
        return heads;
    }

    /// <summary>Reads the tags' paths afresh, before a command renders anything.</summary>
    public async Task RefreshTagPathsAsync() => TagPaths = (await GroupsAsync(heads: false)).Paths;

    public static string Render(ContextFragmentEntity f, int maxContent = int.MaxValue, string? self = null, DateTimeOffset? now = null, int maxReason = 80)
    {
        var flags = new List<string>();
        if (self is not null && f.SessionId is not null && f.SessionId != self) flags.Add($"session {Short(f.SessionId)}");
        if (f.IsProtected) flags.Add("protected");
        if (f.SupersededById is { } by)
            flags.Add(Heads.TryGetValue(f.Id, out var head) && head.Hops >= 1 ? $"superseded, current #{head.Head}" : $"superseded by #{by}");
        else if (f.Status == ContextFragmentStatus.Archived) flags.Add("archived");
        if (f.ClosedUtc is { } closed) flags.Add(ClosedLabel(closed, f.Reason, f.EndedBy, maxReason));
        if (f.IsDeleted) flags.Add("forgotten");
        if (f.Tags.Count > 0) flags.Add("tags: " + string.Join(", ", f.Tags.Select(t => TagPaths.GetValueOrDefault(t.Id, t.Name))));

        var header = $"[#{f.Id} | {f.FragmentType} | i:{f.Importance:0.##} c:{f.Confidence:0.##}{(flags.Count > 0 ? " | " + string.Join(" | ", flags) : "")}]";
        if (f.FragmentType == ContextFragmentType.ChatMessage)
        {
            var who = f.Sources.FirstOrDefault(s => s.Name is not null)?.Name ?? "?";
            var how = new List<string>();
            if (f.EnteredBy is not null) how.Add($"entered as {f.EnteredBy}");
            if (f.AttributedNote is not null) how.Add($"attributed {f.AttributedNote}");
            header += $" {who}{(how.Count > 0 ? $" ({string.Join("; ", how)})" : "")}, {f.CreatedUtc.ToUniversalTime():yyyy-MM-dd HH:mm}Z{Ago(f.CreatedUtc, now)}:";
        }
        else if (now is not null)
        {
            // Recall gives the minute and the age (Statekeeper's freshness, first cut, room 1572709639043289180): a record
            // that states a fact as current reads as what was so then. Only the age: a fragment is a record of what was
            // said, not a state that goes stale, and nothing confirms one yet (Claude and Ember, #5178). No "changed":
            // LastModifiedUtc moves on any edit, a re-tag or a recall-only mark included, so it would make an old claim
            // look freshly changed when only its filing did (Ember, room 1572712681214050409).
            header = header[..^1] + $" | written {f.CreatedUtc.ToUniversalTime():yyyy-MM-dd HH:mm}Z{Ago(f.CreatedUtc, now)}]";
        }
        else
        {
            // When a view was written, and last changed, is what tells a current belief from an old one.
            var written = f.CreatedUtc.ToUniversalTime().ToString("yyyy-MM-dd");
            var changed = f.LastModifiedUtc.ToUniversalTime().ToString("yyyy-MM-dd");
            header = header[..^1] + $" | {written}{(changed != written ? $", changed {changed}" : "")}]";
        }

        var content = f.Content.Length <= maxContent
            ? f.Content
            : f.Content[..maxContent] + $"… [#{f.Id} continues: `show {f.Id}`]";
        return $"{header}\n{content}";
    }

    /// <summary>How long before <paramref name="now"/> a time was, as " (1h ago)", or " (from the future)" if after it; nothing without a clock.</summary>
    private static string Ago(DateTimeOffset at, DateTimeOffset? now)
    {
        if (now is not { } clock)
        {
            return "";
        }

        var age = clock - at;
        // A time ahead of the clock is skew between clocks, not freshness: say so rather than fold it into "just now".
        return age < TimeSpan.Zero ? " (from the future)"
            : age.TotalMinutes < 1 ? " (just now)"
            : age.TotalMinutes < 60 ? $" ({(int)age.TotalMinutes}m ago)"
            : age.TotalHours < 48 ? $" ({(int)age.TotalHours}h ago)"
            : $" ({(int)age.TotalDays}d ago)";
    }

    /// <summary>
    /// Corrects who wrote an utterance already kept, without rewriting it: the writer becomes
    /// <paramref name="writtenBy"/>, the old writer is kept as the login that entered it, and who corrected it,
    /// when and why is kept with it and shown in its header. Only an utterance the local side said; the peer's
    /// own words are its own (#839: Claude's words kept as John's, 2026-11-09).
    /// </summary>
    public async Task<ContextFragmentEntity> AttributeAsync(long id, string writtenBy, string reason)
    {
        var said = await GetAsync(id);
        var peer = await PeerSourceAsync();
        if (said.FragmentType != ContextFragmentType.ChatMessage)
        {
            throw new RefusedException($"#{id} is a {said.FragmentType}, not an utterance: only what was said has a writer to correct.");
        }

        if (said.Sources.Any(s => s.Id == peer.Id))
        {
            throw new RefusedException($"#{id} is {peer.Name}'s own words: attribute corrects who on the other side said something.");
        }

        if (string.IsNullOrWhiteSpace(reason))
        {
            throw new UsageException("attribute needs --reason: a correction to the record says why.");
        }

        var was = said.Sources.FirstOrDefault(s => s.Name is not null)?.Name ?? "?";
        if (string.Equals(was, writtenBy, StringComparison.OrdinalIgnoreCase))
        {
            throw new RefusedException($"#{id} is already {was}'s.");
        }

        var writer = await sources.EnsureLocalPeerSourceAsync(writtenBy);
        said.EnteredBy ??= was;
        said.AttributedNote = $"by {peer.Name}, {DateTimeOffset.UtcNow:yyyy-MM-dd}, was {was}: {reason.Trim()}";
        said.Sources = [new SourceEntity { Id = writer, SourceType = SourceType.HumanPeer, Name = writtenBy, CreatedUtc = said.CreatedUtc, LastModifiedUtc = said.CreatedUtc }];
        await fragments.SaveAsync(said);
        return said;
    }

    private async Task<ContextFragmentEntity> WritableAsync(long id)
    {
        var fragment = await GetAsync(id);
        return fragment.IsProtected
            ? throw new RefusedException($"#{id} is protected: the local peer set it, and only they change it.")
            : fragment;
    }

    private async Task<SourceEntity> PeerSourceAsync() =>
        (await sources.GetAllAsync()).First(s => s.Id == session.RemotePeerSourceId);

    private async Task<List<TagEntity>> TagsAsync(IReadOnlyList<string> paths)
    {
        var found = new List<TagEntity>();
        var made = false;
        foreach (var given in paths)
        {
            // A project is named by its folder, lower-cased (`project/virtuousv2`); a hand-typed
            // `project/VirtuousV2` made a second tag for the same project (the work Claude, 2026-11-09).
            var path = given.StartsWith(ProjectTag + "/", StringComparison.OrdinalIgnoreCase) ? given.ToLowerInvariant() : given;
            var (tag, created) = await tags.GetOrCreateByPathAsync(path);
            made |= created;
            if (tag is not null)
            {
                found.Add(tag);
            }
        }

        // A tag made by this command has no path in the cache yet, so `add` echoed `pdf/rollout` as `rollout`.
        if (made)
        {
            await RefreshTagPathsAsync();
        }

        return found;
    }

    private async Task<TagEntity?> FindTagAsync(string path)
    {
        TagEntity? tag = null;
        foreach (var name in path.Split('/', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
        {
            tag = await tags.GetByNameAsync(name, tag?.Id);
            if (tag is null)
            {
                return null;
            }
        }

        return tag;
    }

    /// <summary>Loads fragments with their sources and tags, in the order of <paramref name="ids"/>.</summary>
    private async Task<IReadOnlyList<ContextFragmentEntity>> HydratedAsync(IReadOnlyList<long> ids)
    {
        if (ids.Count == 0)
        {
            return [];
        }

        var byId = (await fragments.GetByIdsAsync(ids)).ToDictionary(f => f.Id);
        return ids.Where(byId.ContainsKey).Select(i => byId[i]).ToList();
    }
}
