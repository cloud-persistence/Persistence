using System.Text;
using Persistence.Config;
using Persistence.Store;

namespace Persistence.Tests;

/// <summary>
/// RED ON PURPOSE (outstanding work, not a regression): a decomposed é (e + U+0301) and a precomposed é (U+00E9) are
/// the same word to a reader but not to the store. Arden's non-blocking note on #92 at 5d4ec04 (room
/// 1575150835627991140): the gap predates #92 (WordPattern and the stems split on letters and digits, and U+0301 is a
/// combining mark, so a decomposed "Fléchette" splits to "fle" and "chette"). The fix Arden named: NFC at the store's
/// entry, so content, concept names and recall queries are compared in one form. docs/TODO.md, "Found in use", has the item.
/// </summary>
public sealed class NfcNormalisationTests : IDisposable
{
    private const string Precomposed = "Fl\u00e9chette";   // Fléchette (a glider), é as one code point
    private const string Decomposed = "Fle\u0301chette";   // the same word, e then a combining acute accent

    private readonly string db = Path.Combine(Path.GetTempPath(), $"nfc-{Guid.NewGuid():N}.db");

    public void Dispose() => TestDatabase.Cleanup(db);

    private Task<FragmentStore> OpenAsync()
    {
        var config = new AppConfig { PeerName = "Claude" };
        config.DatabasePath = db;
        return FragmentStore.OpenAsync(config);
    }

    [Fact]
    public void The_two_forms_differ_as_strings_and_agree_under_NFC()
    {
        // The premise, so a red test below can't be explained by a typo in the constants.
        Assert.NotEqual(Precomposed, Decomposed);
        Assert.Equal(Precomposed, Decomposed.Normalize(NormalizationForm.FormC));
    }

    [Fact]
    public void Stems_of_either_form_are_the_same()
    {
        var precomposed = FragmentStore.Stems($"{Precomposed} reviewed the glider wing.");
        var decomposed = FragmentStore.Stems($"{Decomposed} reviewed the glider wing.");
        Assert.Equal(precomposed.OrderBy(w => w, StringComparer.Ordinal), decomposed.OrderBy(w => w, StringComparer.Ordinal));
    }

    [Fact]
    public async Task Content_stored_decomposed_reads_back_in_NFC()
    {
        var store = await OpenAsync();
        var added = await store.AddAsync("Personal", $"{Decomposed} reviewed the glider wing.", null, 0.5f, null, [], false, null);
        var read = await store.GetAsync(added.Id);
        Assert.True(read.Content.IsNormalized(NormalizationForm.FormC), "the store kept the decomposed form");
    }

    [Theory]
    [InlineData(Precomposed, Precomposed)]
    [InlineData(Precomposed, Decomposed)]
    [InlineData(Decomposed, Precomposed)]
    public async Task A_concept_named_in_one_form_is_recalled_from_a_message_in_the_other(string named, string said)
    {
        var store = await OpenAsync();
        for (var i = 0; i < 5; i++)
        {
            await store.AddAsync("Personal", $"Routine note {i}: the main branch was reviewed and merged.", null, 0.5f, null, [], false, null);
        }

        var source = await store.AddAsync("Personal", $"{named} reviewed the glider wing for a heavy pilot.", null, 0.5f, null, [], false, null);
        await store.ConceptAsync(named, "thing");
        await store.ConfirmKindAsync(named, "thing");
        var accepted = await store.AddFactAsync(named, "reviewed the glider wing for a heavy pilot", [source.Id]);
        Assert.Equal("active", accepted.Status);

        var recall = await store.ConceptRecallAsync($"{said}: what did you make of the glider wing?", floor: 0.01);
        Assert.Contains($"#{accepted.Id} reviewed the glider wing", recall.Top);
    }
}
