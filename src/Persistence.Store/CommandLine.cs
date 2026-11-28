namespace Persistence.Store;

/// <summary>
/// The parsed command line: the command, its positional arguments, and its <c>--name value</c>
/// options. A name in <see cref="Flags"/> takes no value; every other <c>--name</c> takes the next
/// token, and may repeat (<c>--tag a --tag b</c>).
/// </summary>
internal sealed class CommandLine
{
    /// <summary>The options that are switches rather than carrying a value.</summary>
    /// <summary>Every option some command reads (a text option's <c>-file</c> form too); anything else is refused.</summary>
    public static readonly IReadOnlySet<string> Known = new HashSet<string>(
        ["archived", "batch", "before", "compacted", "confidence", "db", "debug", "ended", "everywhere", "exclude", "help", "importance", "json", "newer", "newest",
         "hours", "limit", "local-peer", "max", "max-chars", "over", "peer", "port", "project", "propose", "protected", "reason", "recent", "said", "sample", "session",
         "snippet", "speaker", "tag", "transcript", "type", "untag", "wander", "when", "written-by", "content", "content-file", "summary", "summary-file", "text", "text-file",
         "fact", "fact-file", "source", "object", "state", "as-of", "supersedes", "kind", "by", "note", "history", "after", "concepts", "facts", "budget", "ambiguous", "split", "proposed", "floor", "move", "flag", "socket", "allow-uid", "tcp-no-auth", "diagnostics", "ended-by", "apply", "model"],
        StringComparer.OrdinalIgnoreCase);

    public static readonly IReadOnlySet<string> Flags = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
    {
        "json", "newer", "newest", "protected", "archived", "help", "debug", "everywhere", "propose", "state", "history", "move", "tcp-no-auth", "diagnostics", "split", "apply",
    };

    public string Command { get; }

    public IReadOnlyList<string> Positional { get; }

    private readonly Dictionary<string, List<string>> options;

    private CommandLine(string command, List<string> positional, Dictionary<string, List<string>> options)
    {
        Command = command;
        Positional = positional;
        this.options = options;
    }

    public static CommandLine Parse(IReadOnlyList<string> args)
    {
        var positional = new List<string>();
        var options = new Dictionary<string, List<string>>(StringComparer.OrdinalIgnoreCase);

        for (var i = 0; i < args.Count; i++)
        {
            var arg = args[i];

            if (!arg.StartsWith("--", StringComparison.Ordinal) || arg == "--")
            {
                positional.Add(arg);
                continue;
            }

            var name = arg[2..];
            string? value = null;
            if (name.Split('=')[0] is var asked && !Known.Contains(asked))
            {
                // An option no command reads was ignored without a word: `--tags a,b` for `--tag a/b` left a day
                // of fragments untagged (2026-11-08). Now it is refused before anything is written.
                throw new UsageException($"--{asked} is not an option of any command.");
            }

            var eq = name.IndexOf('=');
            if (eq >= 0)
            {
                value = name[(eq + 1)..];
                name = name[..eq];
            }
            else if (!Flags.Contains(name))
            {
                if (i + 1 >= args.Count)
                {
                    throw new UsageException($"--{name} needs a value.");
                }

                value = args[++i];
            }

            if (!options.TryGetValue(name, out var values))
            {
                options[name] = values = [];
            }

            values.Add(value ?? "true");
        }

        var command = positional.Count > 0 ? positional[0] : "help";
        return new CommandLine(command, positional.Skip(1).ToList(), options);
    }

    public bool Has(string name) => options.ContainsKey(name);

    public string? Get(string name) => options.TryGetValue(name, out var v) ? v[^1] : null;

    public IReadOnlyList<string> GetAll(string name) => options.TryGetValue(name, out var v) ? v : [];

    public int GetInt(string name, int fallback) =>
        Get(name) is { } raw
            ? int.TryParse(raw, out var n) ? n : throw new UsageException($"--{name} must be a whole number, not '{raw}'.")
            : fallback;

    public float? GetFloat(string name)
    {
        if (Get(name) is not { } raw)
        {
            return null;
        }

        if (!float.TryParse(raw, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var f)
            || f < 0 || f > 1)
        {
            throw new UsageException($"--{name} must be a number from 0 to 1, not '{raw}'.");
        }

        return f;
    }

    public long Id(int position = 0)
    {
        if (Positional.Count <= position)
        {
            throw new UsageException($"'{Command}' needs a fragment id.");
        }

        var raw = Positional[position].TrimStart('#');
        return long.TryParse(raw, out var id) ? id : throw new UsageException($"'{Positional[position]}' is not a fragment id.");
    }

    /// <summary>
    /// The text a command was given: <c>--content</c>, where <c>-</c> means standard input, or
    /// <c>--content-file</c>. Standard input and a file keep multi-line text intact through a shell.
    /// </summary>
    public string? Text(string name = "content")
    {
        if (Get($"{name}-file") is { } path)
        {
            return File.ReadAllText(path);
        }

        var value = Get(name);
        return value == "-" ? Console.In.ReadToEnd() : value;
    }
}

/// <summary>A mistake in how the CLI was called: printed with the usage, exit code 1.</summary>
internal sealed class UsageException(string message) : Exception(message);

/// <summary>A request the store refuses or cannot meet (no such fragment, a protected one): exit code 2.</summary>
internal sealed class RefusedException(string message) : Exception(message);
