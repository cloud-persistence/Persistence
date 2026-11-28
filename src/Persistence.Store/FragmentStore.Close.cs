using Persistence.Data.Entities;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Persistence.Store;

/// <summary>
/// A split chain that can't give an assertion date: a <c>SplitFromId</c> naming a fragment that doesn't exist, a
/// fragment naming itself, a cycle, or a chain past the hop limit. Refused, never guessed: falling back to the last
/// date reached, or to the fragment's own, could manufacture an assertion date (build plan A3, Ember).
/// </summary>
internal sealed class ProvenanceException(string message) : Exception(message);

/// <summary>
/// Close (docs/design/close-state.md, built to docs/design/close-build-plan.md PR A): marking a fragment written as a
/// present state no longer true, with when, why and what ended it. The fragment keeps its content, type and status;
/// it leaves the core and comes back from recall labelled. Reopen undoes it, with its own why. The columns hold the
/// current close only; the audit log, which snapshots every save, keeps every close and reopen.
/// </summary>
internal sealed partial class FragmentStore
{
    private const string CloseAsleep =
        "A sleep pass doesn't close or reopen: the night drafts a close; a waking self runs it (close-state.md §5).";

    /// <summary>
    /// The longest <c>SplitFromId</c> chain followed before it's called broken: the same guard as the supersede walk's,
    /// far past any real chain. A test lowers it rather than writing a thousand fragments.
    /// </summary>
    public int SplitHopLimit { get; set; } = 1000;

    /// <summary>The note <see cref="SplitAsync"/> writes on an original, anchored: "Split into #a, #b" then "." or ": why".</summary>
    private static readonly Regex SplitNote = new(@"^Split into (#\d+(?:, #\d+)+)(?:\.\z|: )", RegexOptions.CultureInvariant);

    /// <summary>The three ended-by forms: a fragment, a commit, a room message. Nothing else is stored.</summary>
    private static readonly Regex EndedByForm = new(@"\A(?:#(?<id>\d+)|commit:[^@\s]+@[0-9a-fA-F]{7,40}|msg:\d+)\z", RegexOptions.CultureInvariant);

    /// <summary>
    /// Closes <paramref name="id"/>: the state it records ended, because of <paramref name="endedBy"/> (required unless it
    /// was written with <c>--state</c>), for <paramref name="reason"/> (always required). Each refusal says what to do.
    /// The input is checked before anything is read, so a malformed command is a <see cref="UsageException"/> (exit 1)
    /// whatever the store holds; everything after is the store's <see cref="RefusedException"/> (exit 2).
    /// </summary>
    public async Task<ContextFragmentEntity> CloseAsync(long id, string? endedBy, string? reason)
    {
        if (string.IsNullOrWhiteSpace(reason))
        {
            throw new UsageException("close needs --reason: why this event ended this state. An ended-by alone proves an event exists, not that it ended anything.");
        }

        endedBy = string.IsNullOrWhiteSpace(endedBy) ? null : endedBy.Trim();
        var form = endedBy is null ? null : EndedByForm.Match(endedBy);
        if (form is { Success: false })
        {
            throw new UsageException($"'{endedBy}' isn't an ended-by: use #<fragment id>, commit:<repo>@<7 to 40 hex> or msg:<room message id>.");
        }

        if (await OpenPassAsync(includeExpired: false) is not null)
        {
            throw new RefusedException(CloseAsleep);
        }

        var fragment = await RememberedAsync(id, "closed");
        if (fragment.FragmentType == ContextFragmentType.WorkingNote)
        {
            throw new RefusedException($"#{id} is the working note: `note` replaces it, it isn't closed.");
        }

        if (fragment.FragmentType == ContextFragmentType.Identity)
        {
            throw new RefusedException($"#{id} is Identity: values and positions change by supersede, they don't end.");
        }

        if (fragment.FragmentType == ContextFragmentType.ChatMessage)
        {
            throw new RefusedException($"#{id} is a ChatMessage: what was said stays said. Write the state it carried as its own "
                + "fragment (`add --state`) and close that.");
        }

        if (fragment.ClosedUtc is { } at)
        {
            throw new RefusedException($"#{id} is already closed ({ClosedLabel(at, fragment.Reason, fragment.EndedBy, 80)}): reopen it first.");
        }

        if (fragment.SupersededById is { } by)
        {
            // A split original: the state usually sits in a later part, not at the chain's head, so name them all.
            if (await SplitPartsAsync(fragment) is { Count: > 0 } parts)
            {
                throw new RefusedException($"#{id} was split into {string.Join(", ", parts.Select(p => $"#{p}"))}: close the part that holds the state.");
            }

            var head = (await fragments.GetHeadsAsync([id])).TryGetValue(id, out var found) ? found.HeadId : by;
            throw new RefusedException($"#{id} is superseded: close the head of its chain, #{head}.");
        }

        // Every close, cited #id or not (call (a), the merged plan): a broken chain refuses before anything is written.
        var asserted = await AssertionDateOrRefuseAsync(id);
        if (endedBy is null && !fragment.IsState)
        {
            throw new RefusedException($"close needs --ended-by (#<id>, commit:<repo>@<sha> or msg:<id>): #{id} wasn't written with --state, so name the event that ended it.");
        }

        if (form is { Success: true } && form.Groups["id"].Success)
        {
            var cited = long.Parse(form.Groups["id"].Value);
            var ending = await fragments.GetByIdAsync(cited) ?? throw new RefusedException($"There is no fragment #{cited} to have ended #{id}.");
            if (ending.IsDeleted)
            {
                throw new RefusedException($"#{cited} is forgotten: an ending has to be one the store still holds (`unforget {cited}` first, if it is the one).");
            }

            if (cited == id)
            {
                throw new RefusedException($"#{id} can't be what ended itself.");
            }

            // Assertion date against assertion date (F2): a split part of an earlier event would otherwise pass as newer.
            var happened = await AssertionDateOrRefuseAsync(cited);
            if (happened <= asserted)
            {
                throw new RefusedException($"#{cited} (asserted {happened.ToUniversalTime():yyyy-MM-dd HH:mm:ss}Z) isn't after #{id} (asserted "
                    + $"{asserted.ToUniversalTime():yyyy-MM-dd HH:mm:ss}Z), so it can't be what ended it.");
            }
        }

        fragment.ClosedUtc = DateTimeOffset.UtcNow;
        fragment.Reason = reason.Trim();
        fragment.EndedBy = endedBy;
        fragment.ReopenReason = null;
        Touch(fragment);
        await fragments.SaveAsync(fragment);
        return fragment;
    }

    /// <summary>Undoes a close, with its own why: the columns clear, the why is kept on <c>ReopenReason</c> and in the log.</summary>
    public async Task<ContextFragmentEntity> ReopenAsync(long id, string? reason)
    {
        if (string.IsNullOrWhiteSpace(reason))
        {
            throw new UsageException("reopen needs --reason: why the state holds again (for example, the merge was reverted).");
        }

        if (await OpenPassAsync(includeExpired: false) is not null)
        {
            throw new RefusedException(CloseAsleep);
        }

        var fragment = await RememberedAsync(id, "reopened");
        if (fragment.ClosedUtc is null)
        {
            throw new RefusedException($"#{id} isn't closed.");
        }

        fragment.ClosedUtc = null;
        fragment.Reason = null;
        fragment.EndedBy = null;
        fragment.ReopenReason = reason.Trim();
        Touch(fragment);
        await fragments.SaveAsync(fragment);
        return fragment;
    }

    /// <summary>
    /// <see cref="WritableAsync"/> and not forgotten: a forgotten fragment was deliberately taken out of reads, so close
    /// and reopen don't change it behind that (Ember, #96 point 1); `unforget` first, as a cited ending already needs.
    /// </summary>
    private async Task<ContextFragmentEntity> RememberedAsync(long id, string done)
    {
        var fragment = await WritableAsync(id);
        return fragment.IsDeleted
            ? throw new RefusedException($"#{id} is forgotten: `unforget {id}` first if it should be {done}.")
            : fragment;
    }

    /// <summary>
    /// An edit, so it moves the fields together: <c>TouchedSessionId</c> is the session that last added or edited it, and
    /// <c>TouchedModelId</c> the model. The model is this call's, null when it did not say: kept from the last editor, it
    /// would put this one's change under another model's name.
    /// </summary>
    private void Touch(ContextFragmentEntity fragment)
    {
        fragment.LastModifiedUtc = DateTimeOffset.UtcNow;
        fragment.TouchedSessionId = Session ?? fragment.TouchedSessionId;
        fragment.TouchedModelId = Model;
    }

    /// <summary>
    /// When <paramref name="id"/>'s content was first asserted: follow <c>SplitFromId</c> to the unsplit fragment and take
    /// its <c>CreatedUtc</c>. Never back through supersede, which is a new assertion (close-state.md §2). A forgotten
    /// fragment on the way still counts: its date is real. A broken chain throws <see cref="ProvenanceException"/>.
    /// </summary>
    public async Task<DateTimeOffset> AssertionDateAsync(long id)
    {
        var current = await fragments.GetByIdAsync(id) ?? throw Broken(id, [id], $"#{id} is missing");
        var chain = new List<long> { id };
        while (current.SplitFromId is { } from)
        {
            if (from == current.Id)
            {
                throw Broken(id, chain, $"#{current.Id} names itself as what it was split from");
            }

            if (chain.Contains(from))
            {
                throw Broken(id, [.. chain, from], $"a cycle back to #{from}");
            }

            if (chain.Count > SplitHopLimit)
            {
                throw Broken(id, chain, $"past the hop limit of {SplitHopLimit}");
            }

            current = await fragments.GetByIdAsync(from)
                ?? throw Broken(id, [.. chain, from], $"#{current.Id} was split from #{from}, which is missing");
            chain.Add(from);
        }

        return current.CreatedUtc;
    }

    private static ProvenanceException Broken(long id, IReadOnlyList<long> chain, string which) =>
        new($"provenance of #{id} is broken ({which}); chain read: {string.Join(" → ", chain.Select(c => $"#{c}"))}");

    private async Task<DateTimeOffset> AssertionDateOrRefuseAsync(long id)
    {
        try
        {
            return await AssertionDateAsync(id);
        }
        catch (ProvenanceException broken)
        {
            throw new RefusedException($"{broken.Message}: fix the chain before closing.");
        }
    }

    /// <summary>The parts <paramref name="original"/> was split into: its back-links, or for a split made before them, its note.</summary>
    private async Task<IReadOnlyList<long>> SplitPartsAsync(ContextFragmentEntity original)
    {
        var linked = (await fragments.GetSplitPartsAsync(original.Id)).Select(p => p.Id).ToList();
        return linked.Count > 0 ? linked : SplitNoteIds(original.Notes) ?? [];
    }

    private static List<long>? SplitNoteIds(string? notes) =>
        SplitNote.Match(notes ?? "") is { Success: true } m
            ? m.Groups[1].Value.Split(", ").Select(x => long.Parse(x.TrimStart('#'))).ToList()
            : null;

    /// <summary>The label a closed fragment carries wherever it's rendered: <c>closed &lt;date&gt;: &lt;reason&gt; (ended by &lt;ref&gt;)</c>.</summary>
    private static string ClosedLabel(DateTimeOffset closed, string? reason, string? endedBy, int maxReason) =>
        $"closed {closed.ToUniversalTime():yyyy-MM-dd}: {Clip(reason ?? "", maxReason)}{(endedBy is null ? "" : $" (ended by {endedBy})")}";

    /// <summary>How many earlier texts <c>show</c> prints in full before pointing at <c>--history</c>: a working note is rewritten every block.</summary>
    public const int EarlierTextsShown = 3;

    /// <summary>
    /// Every text <paramref name="id"/> had before an edit replaced it in place, oldest first, from the audit log's
    /// before-and-after snapshots, with when, which session and (from migration 022) which model: an edit leaves no
    /// archived fragment as supersede does, so this is the only place the original can be read back (Arden's borrow,
    /// room 1576023465000181972). An edit that left the content and summary alone (a re-tag, a new importance) is not one.
    /// </summary>
    /// <summary>
    /// Every close and reopen of <paramref name="id"/>, oldest first, read from the audit log's snapshots (one line per
    /// change of <c>ClosedUtc</c>): what happened, not what the columns say now.
    /// </summary>
    private async Task<IReadOnlyList<string>> EarlierTextsAsync(long id)
    {
        var lines = new List<string>();
        foreach (var entry in await audits.GetByTargetAsync(nameof(ContextFragmentEntity), id))
        {
            if (entry.OldData is null || entry.NewData is null)
            {
                continue;
            }

            using var was = JsonDocument.Parse(entry.OldData);
            using var now = JsonDocument.Parse(entry.NewData);
            string? Read(JsonDocument json, string name) =>
                json.RootElement.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
            var (oldContent, oldSummary) = (Read(was, nameof(ContextFragmentEntity.Content)), Read(was, nameof(ContextFragmentEntity.Summary)));
            if (oldContent == Read(now, nameof(ContextFragmentEntity.Content)) && oldSummary == Read(now, nameof(ContextFragmentEntity.Summary)))
            {
                continue;
            }

            var by = new List<string> { $"session {Short(entry.SessionId)}" };
            if (Read(now, nameof(ContextFragmentEntity.TouchedModelId)) is { } model)
            {
                by.Add(model);
            }

            lines.Add($"{entry.CreatedUtc.ToUniversalTime():yyyy-MM-dd HH:mm}Z, replaced ({string.Join(", ", by)}): {oldContent}"
                + (oldSummary is { Length: > 0 } summary && summary != Read(now, nameof(ContextFragmentEntity.Summary)) ? $"\n  (summary was: {summary})" : ""));
        }

        return lines;
    }

    private async Task<IReadOnlyList<string>> CloseHistoryAsync(long id)
    {
        var lines = new List<string>();
        string? was = null;
        foreach (var entry in await audits.GetByTargetAsync(nameof(ContextFragmentEntity), id))
        {
            if (entry.NewData is null)
            {
                continue;
            }

            using var json = JsonDocument.Parse(entry.NewData);
            string? Read(string name) =>
                json.RootElement.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
            var closed = Read(nameof(ContextFragmentEntity.ClosedUtc));
            if (closed == was)
            {
                continue;
            }

            var when = $"{entry.CreatedUtc.ToUniversalTime():yyyy-MM-dd HH:mm}Z";
            lines.Add(closed is null
                ? $"{when} reopened: {Read(nameof(ContextFragmentEntity.ReopenReason))}"
                : $"{when} closed: {Read(nameof(ContextFragmentEntity.Reason))}"
                  + (Read(nameof(ContextFragmentEntity.EndedBy)) is { } by ? $" (ended by {by})" : ""));
            was = closed;
        }

        return lines;
    }

    /// <summary>
    /// Links the splits made before <c>SplitFromId</c> existed, from the notes <see cref="SplitAsync"/> wrote
    /// (<c>backfill-splits [--apply]</c>; build plan A2). A note links only if every check passes; anything else is
    /// reported and left alone, never guessed. Idempotent by state: parts already pointing at their original read
    /// "already correct". Without <paramref name="apply"/> nothing is written.
    /// </summary>
    public async Task<string> BackfillSplitsAsync(bool apply)
    {
        if (apply && await OpenPassAsync(includeExpired: false) is not null)
        {
            throw new RefusedException("A sleep pass doesn't link splits: run `backfill-splits` without --apply and propose the apply in the record.");
        }

        var lines = new List<string>();
        var notes = new List<(ContextFragmentEntity Original, List<long> Parts)>();
        foreach (var f in await fragments.GetByNotesContainingAsync("split"))
        {
            if (SplitNoteIds(f.Notes) is { } ids)
            {
                notes.Add((f, ids));
            }
            else
            {
                lines.Add($"#{f.Id}: not read, its note mentions \"split\" but isn't a split note: {Clip(f.Notes ?? "", 100)}");
            }
        }

        var named = notes.SelectMany(n => n.Parts.Distinct()).GroupBy(p => p).ToDictionary(g => g.Key, g => g.Count());
        int linked = 0, correct = 0, skipped = 0;
        foreach (var (original, ids) in notes)
        {
            var (parts, why) = await CheckSplitAsync(original, ids, named);
            if (why is not null)
            {
                skipped++;
                lines.Add($"#{original.Id}: skipped ({why}); note: {Clip(original.Notes ?? "", 100)}");
                continue;
            }

            var list = string.Join(", ", parts.Select(p => $"#{p.Id}"));
            if (parts.All(p => p.SplitFromId == original.Id))
            {
                correct++;
                lines.Add($"#{original.Id}: already correct ({parts.Count} parts)");
                continue;
            }

            linked++;
            if (apply)
            {
                // One transaction per note (Ember, #96 point 4): a failed write leaves every part of this note
                // unlinked, never the partial state a later run refuses to repair. Notes before it stay committed.
                await fragments.RunInTransactionAsync(async transaction =>
                {
                    foreach (var part in parts)
                    {
                        part.SplitFromId = original.Id;
                        await fragments.SaveAsync(part, transaction);
                    }

                    return parts.Count;
                });
            }

            lines.Add($"#{original.Id}: {(apply ? "linked" : "would link")} {parts.Count} parts ({list})");
        }

        lines.Add($"{notes.Count} split notes: {linked} {(apply ? "linked" : "to link")}, {correct} already correct, {skipped} skipped"
            + (apply ? "." : ". Dry run: nothing written; --apply links them."));
        return string.Join('\n', lines);
    }

    /// <summary>The parts of one split note if every check passes, else why not. Parts already linked are a pass only all to the original.</summary>
    private async Task<(IReadOnlyList<ContextFragmentEntity> Parts, string? Why)> CheckSplitAsync(
        ContextFragmentEntity original, IReadOnlyList<long> ids, IReadOnlyDictionary<long, int> named)
    {
        if (ids.Distinct().Count() != ids.Count)
        {
            return ([], "an id appears twice in the note");
        }

        if (original.SupersededById != ids[0])
        {
            return ([], $"the first id, #{ids[0]}, isn't what #{original.Id} was superseded by ({(original.SupersededById is { } s ? $"#{s}" : "nothing")})");
        }

        if (ids.FirstOrDefault(p => named[p] > 1) is var twice and > 0)
        {
            return ([], $"#{twice} is named in another split note too");
        }

        try
        {
            await AssertionDateAsync(original.Id);
        }
        catch (ProvenanceException broken)
        {
            return ([], broken.Message);
        }

        var parts = (await HydratedAsync(ids)).ToDictionary(p => p.Id);
        foreach (var id in ids)
        {
            var why = !parts.TryGetValue(id, out var part) ? $"#{id} is missing"
                : part.IsDeleted ? $"#{id} is forgotten"
                : part.FragmentType != original.FragmentType ? $"#{id} is a {part.FragmentType}, the original a {original.FragmentType}"
                : part.CreatedUtc < original.CreatedUtc ? $"#{id} is older than the original"
                : null;
            if (why is not null)
            {
                return ([], why);
            }
        }

        var links = ids.Select(i => parts[i].SplitFromId).ToList();
        if (links.All(l => l is null) || links.All(l => l == original.Id))
        {
            return (ids.Select(i => parts[i]).ToList(), null);
        }

        // Something wrote outside both paths: the owner should look, so it's named and never completed or overwritten.
        var toOriginal = links.Any(l => l == original.Id);
        var unlinked = links.Any(l => l is null);
        var elsewhere = links.Where(l => l is not null && l != original.Id).Select(l => $"#{l}").Distinct().ToList();
        var kind = elsewhere.Count > 0 && toOriginal && unlinked ? "mixed"
            : elsewhere.Count > 0 ? "differently linked"
            : "partial";
        return ([], $"{kind}: parts' SplitFromId are {string.Join(", ", links.Select(l => l is null ? "null" : $"#{l}"))}"
            + (elsewhere.Count > 0 ? $"; {string.Join(", ", elsewhere)} isn't the original" : ""));
    }
}
