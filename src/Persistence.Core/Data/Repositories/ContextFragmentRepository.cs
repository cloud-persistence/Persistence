using Dapper;
using InterpolatedSql.Dapper;
using Persistence.Config;
using Persistence.Data.Entities;
using Persistence.DI;
using Persistence.Runtime;
using System.Data;

namespace Persistence.Data.Repositories;

/// <summary>
/// Repository for <see cref="ContextFragmentEntity"/>. Every load fully populates
/// <see cref="ContextFragmentEntity.Sources"/> and <see cref="ContextFragmentEntity.Tags"/>.
/// </summary>
[Singleton]
public class ContextFragmentRepository : EntityRepository<ContextFragmentEntity>, IContextFragmentRepository
{
    private readonly ISessionContext sessionContext;
    private readonly IEntityTagRepository entityTagRepo;

    /// <summary>
    /// Constructor
    /// </summary>
    public ContextFragmentRepository(IAppConfig config, ISessionContext sessionContext, IEntityTagRepository entityTagRepo)
        : base(config, sessionContext)
    {
        this.sessionContext = sessionContext;
        this.entityTagRepo = entityTagRepo;
    }

    #region Public methods

    /// <summary>
    /// Returns fragments of the given type. Defaults to active only.
    /// Pass <c>activeOnly: false</c> to include all statuses.
    /// </summary>
    public async Task<IEnumerable<ContextFragmentEntity>> GetByTypeAsync(ContextFragmentType type, bool activeOnly = true) =>
        activeOnly
            ? await QueryAsync(
                $"""
                SELECT * FROM ContextFragments
                WHERE FragmentType = {type} AND Status = {ContextFragmentStatus.Active} AND IsDeleted = 0
                """)
            : await QueryAsync(
                $"SELECT * FROM ContextFragments WHERE FragmentType = {type} AND IsDeleted = 0");

    /// <summary>
    /// Returns all non-deleted fragments tagged with the given tag ID, regardless of status
    /// </summary>
    public async Task<IEnumerable<ContextFragmentEntity>> GetByTagAsync(long tagId) =>
        await QueryAsync(
            $"""
            SELECT cf.*
            FROM ContextFragments cf
            JOIN EntityTags et ON cf.Id = et.EntityId AND et.EntityType = {nameof(ContextFragmentEntity)}
            WHERE et.TagId = {tagId} AND cf.IsDeleted = 0
            """);

    /// <summary>Dates are stored as UTC text in this form, so they compare as text.</summary>
    private static string Stored(DateTimeOffset at) => at.ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss.fffzzz");

    /// <inheritdoc />
    public async Task<IReadOnlyList<long>> GetNewestIdsAsync(ContextFragmentType type, int count, IReadOnlyCollection<string> leaveOut) =>
        count <= 0 ? [] : leaveOut.Count == 0
            ? (await QueryAsync<long>($"""
                SELECT Id FROM ContextFragments
                WHERE FragmentType = {type} AND Status = {ContextFragmentStatus.Active} AND IsDeleted = 0
                ORDER BY CreatedUtc DESC LIMIT {count}
                """)).ToList()
            : (await QueryAsync<long>($"""
                SELECT Id FROM ContextFragments
                WHERE FragmentType = {type} AND Status = {ContextFragmentStatus.Active} AND IsDeleted = 0
                  AND (SessionId IS NULL OR SessionId NOT IN {leaveOut.ToList()})
                ORDER BY CreatedUtc DESC LIMIT {count}
                """)).ToList();

    /// <inheritdoc />
    public async Task<IReadOnlyList<long>> GetIdsBySessionsAsync(ContextFragmentType type, IReadOnlyCollection<string> sessions) =>
        sessions.Count == 0 ? [] : (await QueryAsync<long>($"""
            SELECT Id FROM ContextFragments
            WHERE FragmentType = {type} AND Status = {ContextFragmentStatus.Active} AND IsDeleted = 0
              AND SessionId IN {sessions.ToList()}
            """)).ToList();

    /// <inheritdoc />
    public async Task<IReadOnlyDictionary<string, DateTimeOffset>> GetSessionsSinceAsync(ContextFragmentType type, DateTimeOffset since) =>
        (await QueryAsync<(string SessionId, string Newest)>($"""
            SELECT SessionId, MAX(CreatedUtc) AS Newest FROM ContextFragments
            WHERE FragmentType = {type} AND IsDeleted = 0 AND SessionId IS NOT NULL AND CreatedUtc >= {Stored(since)}
            GROUP BY SessionId
            """)).ToDictionary(r => r.SessionId, r => DateTimeOffset.Parse(r.Newest, System.Globalization.CultureInfo.InvariantCulture));

    /// <inheritdoc />
    public async Task<IReadOnlyList<(long Id, string MessageId, string? SessionId)>> GetMessageIdsAsync(ContextFragmentType type) =>
        (await QueryAsync<(long Id, string MessageId, string? SessionId)>($"""
            SELECT Id, MessageId, SessionId FROM ContextFragments
            WHERE FragmentType = {type} AND MessageId IS NOT NULL
            """)).ToList();

    /// <inheritdoc />
    public async Task<IEnumerable<ContextFragmentEntity>> GetSupersededByAsync(long id) =>
        await QueryAsync($"SELECT * FROM ContextFragments WHERE SupersededById = {id} AND IsDeleted = 0");

    /// <inheritdoc />
    public async Task<IEnumerable<ContextFragmentEntity>> GetSplitPartsAsync(long id) =>
        await QueryAsync($"SELECT * FROM ContextFragments WHERE SplitFromId = {id} ORDER BY Id");

    /// <inheritdoc />
    public async Task<IEnumerable<ContextFragmentEntity>> GetByNotesContainingAsync(string word) =>
        await QueryAsync($"SELECT * FROM ContextFragments WHERE Notes LIKE {"%" + word + "%"} ORDER BY Id");

    /// <summary>Longest chain of supersessions followed: a guard against a cycle, far past any real chain.</summary>
    private const int MaxHops = 1000;

    /// <inheritdoc />
    public async Task<IReadOnlyDictionary<long, (long HeadId, int Hops)>> GetHeadsAsync(
        IReadOnlyCollection<long> ids, CancellationToken ct = default)
    {
        if (ids.Count == 0)
        {
            return new Dictionary<long, (long, int)>();
        }

        // Walks SupersededById from each start to the end of its line in one query; the row with the most hops
        // per start is its head. A merge is fine: each start still has one line.
        var rows = await QueryAsync<(long StartId, long HeadId, int Hops)>($"""
            WITH RECURSIVE chain(StartId, CurId, Hops) AS (
                SELECT Id, Id, 0 FROM ContextFragments WHERE Id IN {ids.ToList()}
                UNION ALL
                SELECT chain.StartId, cf.SupersededById, chain.Hops + 1
                FROM chain JOIN ContextFragments cf ON cf.Id = chain.CurId
                WHERE cf.SupersededById IS NOT NULL AND chain.Hops < {MaxHops}
            )
            SELECT StartId, CurId AS HeadId, Hops FROM chain c
            WHERE Hops > 0 AND Hops = (SELECT MAX(Hops) FROM chain c2 WHERE c2.StartId = c.StartId)
            """, ct);

        return rows.GroupBy(r => r.StartId).ToDictionary(g => g.Key, g => (g.First().HeadId, g.First().Hops));
    }

    /// <summary>
    /// Returns up to <paramref name="limit"/> results ordered best-match first.
    /// </summary>
    public async Task<IEnumerable<ContextFragmentEntity>> SearchRelevantAsync(
        string query, int limit = 20, CancellationToken ct = default)
    {
        var rankedIds = await QueryAsync<long>(
            $"""
            SELECT rowid FROM ContextFragments_fts
            WHERE ContextFragments_fts MATCH {query}
            ORDER BY rank
            LIMIT {limit}
            """);

        if (!rankedIds.Any())
        {
            return [];
        }

        // Exclude forgotten (soft-deleted) fragments: the FTS index is built on content, so flipping
        // IsDeleted doesn't drop a row from the index — without this filter a forgotten fragment would
        // still resurface through every relevance-search caller (list_fragments relevant_to, recall).
        var entities = (await GetByIdsAsync(rankedIds, ct))
            .Where(e => !e.IsDeleted)
            .ToDictionary(e => e.Id);

        return rankedIds.Where(id => entities.ContainsKey(id)).Select(id => entities[id]);
    }

    /// <summary>
    /// What a <see cref="PrefixDocumentCountsAsync"/> prefix may hold: letters and decimal digits (Unicode L and Nd). A
    /// caller splitting text into prefixes splits on this, so the two never disagree: "12m²" is a superscript, Unicode No,
    /// which a wider word pattern took and this check refused (#92 at 081f9ce).
    /// </summary>
    public static bool IsPrefixChar(char c) => char.IsLetterOrDigit(c);

    public async Task<(long Total, IReadOnlyDictionary<string, long> Containing)> PrefixDocumentCountsAsync(IReadOnlyCollection<string> prefixes)
    {
        var total = (await QueryAsync<long>($"SELECT COUNT(*) FROM ContextFragments")).Single();
        var containing = new Dictionary<string, long>();
        foreach (var prefix in prefixes.Distinct())
        {
            if (prefix.Length == 0 || !prefix.All(IsPrefixChar))
            {
                throw new ArgumentException($"A prefix is letters and digits only, not '{prefix}'.", nameof(prefixes));
            }

            var match = $"\"{prefix}\"*";
            containing[prefix] = (await QueryAsync<long>($"SELECT COUNT(*) FROM ContextFragments_fts WHERE ContextFragments_fts MATCH {match}")).Single();
        }

        return (total, containing);
    }

    #endregion

    #region Base overrides

    /// <summary>
    /// Loads fragments by ID with their sources and tags populated
    /// </summary>
    protected override async Task<IEnumerable<ContextFragmentEntity>> LoadByIdsAsync(
        IEnumerable<long> ids, IDbConnection connection, CancellationToken ct = default)
    {
        var idList = ids.ToList();

        var fragments = (await connection.SqlBuilder(
            $"SELECT * FROM ContextFragments WHERE Id IN {idList}")
            .QueryAsync<ContextFragmentEntity>(cancellationToken: ct)).ToList();

        if (fragments.Count == 0)
        {
            return fragments;
        }

        var fragmentIds = fragments.Select(f => f.Id).ToList();

        // Sources for all fragments
        var sourceRows = await connection.SqlBuilder(
            $"""
            SELECT cfs.ContextFragmentId, s.*
            FROM ContextFragmentSources cfs
            JOIN Sources s ON cfs.SourceId = s.Id
            WHERE cfs.ContextFragmentId IN {fragmentIds}
            """)
            .QueryAsync<long, SourceEntity, (long FragmentId, SourceEntity Source)>(
                (fragmentId, source) => (fragmentId, source),
                splitOn: "Id",
                cancellationToken: ct);

        var sourcesByFragment = sourceRows
            .GroupBy(x => x.FragmentId)
            .ToDictionary(g => g.Key, g => g.Select(x => x.Source).ToList());

        // Tags for all fragments (via the generic EntityTags table)
        var tagsByFragment = await entityTagRepo.GetTagsForAsync(nameof(ContextFragmentEntity), fragmentIds, connection, ct);

        foreach (var fragment in fragments)
        {
            fragment.Sources = sourcesByFragment.GetValueOrDefault(fragment.Id, []);
            fragment.Tags = tagsByFragment.GetValueOrDefault(fragment.Id, []);
        }

        return fragments;
    }

    /// <summary>
    /// Syncs the ContextFragmentTags junction table to match the in-memory Tags list
    /// </summary>
    protected override async Task SaveSubEntitiesAsync(
        ContextFragmentEntity entity, IDbTransaction transaction, CancellationToken ct = default)
    {
        await ExecuteAsync(
            $"DELETE FROM ContextFragmentSources WHERE ContextFragmentId = {entity.Id}",
            transaction, ct);

        if (entity.Sources.Count == 0)
        {
            entity.Sources.Add(new SourceEntity
            {
                Id = sessionContext.SystemSourceId,
                SourceType = SourceType.System,
                CreatedUtc = entity.CreatedUtc,
                LastModifiedUtc = entity.LastModifiedUtc,
            });
        }

        foreach (var source in entity.Sources)
        {
            await ExecuteAsync(
                $"INSERT INTO ContextFragmentSources (SourceId, ContextFragmentId) VALUES ({source.Id}, {entity.Id})",
                transaction, ct);
        }

        await entityTagRepo.SetTagsAsync(
            nameof(ContextFragmentEntity), entity.Id, entity.Tags.Select(t => t.Id).ToList(), transaction);
    }

    /// <inheritdoc />
    public async Task SetDeletedAsync(long id, bool deleted, string? reason = null, CancellationToken ct = default)
    {
        var now = DateTimeOffset.UtcNow;

        // On forget, record the (optional) reason in the free-form Notes field so list_forgotten can show
        // "why did I let this go" later. On unforget we leave Notes as the historical record.
        if (deleted && !string.IsNullOrWhiteSpace(reason))
        {
            await ExecuteAsync(
                $"UPDATE ContextFragments SET IsDeleted = 1, Notes = {reason.Trim()}, LastModifiedUtc = {now} WHERE Id = {id}",
                ct: ct);
        }
        else
        {
            await ExecuteAsync(
                $"UPDATE ContextFragments SET IsDeleted = {deleted}, LastModifiedUtc = {now} WHERE Id = {id}",
                ct: ct);
        }
    }

    /// <inheritdoc />
    public async Task<IReadOnlyList<ContextFragmentEntity>> GetDeletedAsync(int limit = 20, CancellationToken ct = default) =>
        // Flat query (not the hydrating QueryAsync): the entity reload filters/joins aren't needed for a
        // recovery listing, and going flat avoids re-touching LastAccessed on fragments merely being reviewed.
        (await QueryAsync<ContextFragmentEntity>(
            $"SELECT * FROM ContextFragments WHERE IsDeleted = 1 ORDER BY LastModifiedUtc DESC LIMIT {limit}",
            ct)).ToList();

    /// <inheritdoc />
    public async Task<(int Forgotten, int Archived)> CountAsideAsync(CancellationToken ct = default)
    {
        var forgotten = (await QueryAsync<int>(
            $"SELECT COUNT(*) FROM ContextFragments WHERE IsDeleted = 1", ct)).FirstOrDefault();

        var archived = (await QueryAsync<int>(
            $"SELECT COUNT(*) FROM ContextFragments WHERE IsDeleted = 0 AND Status = {ContextFragmentStatus.Archived}", ct))
            .FirstOrDefault();

        return (forgotten, archived);
    }

    /// <summary>
    /// Returns the INSERT statement for a context fragment
    /// </summary>
    protected override FormattableString GetInsertSql(ContextFragmentEntity entity) =>
        $"""
        INSERT INTO ContextFragments (FragmentType, Status, Content, Summary, LastAccessedUtc, Importance, Confidence, IsProtected, IsDeleted, CreatedUtc, LastModifiedUtc, Notes, AddressedTo, MessageId, RelayDepth, SupersededById, SessionId, TouchedSessionId, EnteredBy, AttributedNote, ClosedUtc, Reason, EndedBy, ReopenReason, IsState, SplitFromId, ModelId, TouchedModelId)
        VALUES ({entity.FragmentType}, {entity.Status}, {entity.Content}, {entity.Summary}, {entity.LastAccessedUtc}, {entity.Importance}, {entity.Confidence}, {entity.IsProtected}, {entity.IsDeleted}, {entity.CreatedUtc}, {entity.LastModifiedUtc}, {entity.Notes}, {entity.AddressedTo}, {entity.MessageId}, {entity.RelayDepth}, {entity.SupersededById}, {entity.SessionId}, {entity.TouchedSessionId}, {entity.EnteredBy}, {entity.AttributedNote}, {entity.ClosedUtc}, {entity.Reason}, {entity.EndedBy}, {entity.ReopenReason}, {entity.IsState}, {entity.SplitFromId}, {entity.ModelId}, {entity.TouchedModelId})
        """;

    /// <summary>
    /// Returns the UPDATE statement for a context fragment
    /// </summary>
    protected override FormattableString GetUpdateSql(ContextFragmentEntity entity) =>
        $"""
        UPDATE ContextFragments
        SET FragmentType = {entity.FragmentType}, Status = {entity.Status}, Content = {entity.Content},
            Summary = {entity.Summary}, LastAccessedUtc = {entity.LastAccessedUtc}, Importance = {entity.Importance},
            Confidence = {entity.Confidence}, IsProtected = {entity.IsProtected}, IsDeleted = {entity.IsDeleted},
            LastModifiedUtc = {entity.LastModifiedUtc}, Notes = {entity.Notes}, AddressedTo = {entity.AddressedTo},
            MessageId = {entity.MessageId}, RelayDepth = {entity.RelayDepth}, SupersededById = {entity.SupersededById}, SessionId = {entity.SessionId},
            TouchedSessionId = {entity.TouchedSessionId}, EnteredBy = {entity.EnteredBy}, AttributedNote = {entity.AttributedNote},
            ClosedUtc = {entity.ClosedUtc}, Reason = {entity.Reason}, EndedBy = {entity.EndedBy}, ReopenReason = {entity.ReopenReason},
            IsState = {entity.IsState}, SplitFromId = {entity.SplitFromId}, ModelId = {entity.ModelId}, TouchedModelId = {entity.TouchedModelId}
        WHERE Id = {entity.Id}
        """;

    #endregion
}
