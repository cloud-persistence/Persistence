using Persistence.Config;
using Persistence.Store;

namespace Persistence.Tests;

/// <summary>
/// Two borrows from the agent survey (Arden, room 1576023465000181972). An edit rewrites a fragment in place; the
/// audit log keeps what it said, but nothing read it back, so a rewritten view lost its original to anyone reading
/// the store. And a fragment did not say which model wrote it, though the store outlives the model reading it.
/// </summary>
[Collection(nameof(FragmentStoreStaticsCollection))]
public sealed class BorrowsTests : IDisposable
{
    private readonly string db = Path.Combine(Path.GetTempPath(), $"store-{Guid.NewGuid():N}.db");

    public void Dispose() => TestDatabase.Cleanup(db);

    private Task<FragmentStore> OpenAsync()
    {
        var config = new AppConfig { PeerName = "Claude" };
        config.DatabasePath = db;
        return FragmentStore.OpenAsync(config);
    }

    [Fact]
    public async Task An_edit_keeps_the_text_it_replaced_where_show_reads_it()
    {
        var store = await OpenAsync();
        var f = await store.AddAsync("Personal", "The trigger is 500k.", null, 0.5f, null, ["store/hooks"], false);
        await store.EditAsync(f.Id, null, "The trigger is 400k.", null, null, null, []);
        await store.EditAsync(f.Id, null, "The trigger is 300k.", null, null, null, []);

        var shown = await store.ShowAsync(f.Id);

        Assert.Contains("The trigger is 300k.", shown);
        Assert.Contains("The trigger is 500k.", shown);
        Assert.Contains("The trigger is 400k.", shown);
        // Oldest first, as close history reads.
        Assert.True(shown.IndexOf("500k", StringComparison.Ordinal) < shown.IndexOf("400k", StringComparison.Ordinal));
    }

    [Fact]
    public async Task An_edit_that_leaves_the_text_alone_adds_no_earlier_version()
    {
        var store = await OpenAsync();
        var f = await store.AddAsync("Personal", "The trigger is 500k.", null, 0.5f, null, ["store/hooks"], false);
        await store.EditAsync(f.Id, null, null, null, 0.9f, null, ["store/more"]);

        Assert.DoesNotContain("Earlier text", await store.ShowAsync(f.Id));
    }

    [Fact]
    public async Task A_group_head_rewritten_keeps_its_earlier_text_too()
    {
        var store = await OpenAsync();
        var head = await store.SetHeadAsync("project/x", "First head.", null);
        await store.SetHeadAsync("project/x", "Second head.", null);

        Assert.Contains("First head.", await store.ShowAsync(head.Id));
    }

    [Fact]
    public async Task What_a_model_writes_carries_its_id_and_show_names_it()
    {
        var store = await OpenAsync();
        store.Model = "claude-opus-5-5";

        var added = await store.AddAsync("Personal", "A view.", null, 0.5f, null, ["store/hooks"], false);
        var successor = await store.SupersedeAsync([added.Id], "A better view.", null, null, null, [], "sharper");
        var parts = await store.SplitAsync(successor.Id, ["Part one.", "Part two."], "two views");

        Assert.Equal("claude-opus-5-5", (await store.GetAsync(added.Id)).ModelId);
        Assert.Equal("claude-opus-5-5", (await store.GetAsync(successor.Id)).ModelId);
        Assert.All(parts, p => Assert.Equal("claude-opus-5-5", p.ModelId));
        Assert.Contains("claude-opus-5-5", await store.ShowAsync(added.Id));
    }

    [Fact]
    public async Task An_edit_by_another_model_keeps_the_writer_and_names_the_editor()
    {
        var store = await OpenAsync();
        store.Model = "claude-opus-4-8";
        var f = await store.AddAsync("Personal", "Old wording.", null, 0.5f, null, ["store/hooks"], false);
        store.Model = "claude-opus-5-5";
        await store.EditAsync(f.Id, null, "New wording.", null, null, null, []);

        Assert.Equal("claude-opus-4-8", (await store.GetAsync(f.Id)).ModelId);
        var shown = await store.ShowAsync(f.Id);
        Assert.Contains("claude-opus-4-8", shown);
        Assert.Contains("claude-opus-5-5", shown);
    }

    [Fact]
    public async Task A_caller_that_names_no_model_gets_none_guessed()
    {
        var store = await OpenAsync();
        var f = await store.AddAsync("Personal", "A view.", null, 0.5f, null, ["store/hooks"], false);

        Assert.Null((await store.GetAsync(f.Id)).ModelId);
        Assert.DoesNotContain("model", await store.ShowAsync(f.Id), StringComparison.OrdinalIgnoreCase);
    }
}
