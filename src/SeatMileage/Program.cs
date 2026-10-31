using System.Diagnostics;
using System.Text.Json;
using SeatMileage;

static string Env(string name) =>
    Environment.GetEnvironmentVariable(name) is { Length: > 0 } v
        ? v
        : throw new InvalidOperationException($"Missing environment variable {name}");

static string? Str(JsonElement e, string name) =>
    e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

var stopwatch = Stopwatch.StartNew();
try
{
    var email = Env("CUPRA_USER");
    var password = Env("CUPRA_PASS");
    var vin = Env("VIN");
    var notionToken = Env("NOTION_TOKEN");
    var databaseId = Env("DATABASE_ID");
    var property = Env("PROPERTY");
    var notionVersion = Environment.GetEnvironmentVariable("NOTION_VERSION") is { Length: > 0 } nv ? nv : "2022-06-28";
    var headed = Environment.GetEnvironmentVariable("HEADED") == "1";

    int mileage;
    await using (var client = await EudaClient.CreateAsync(headed))
    {
        await client.LoginAsync(email, password);

        Console.WriteLine("EUDA: fetching metadata");
        var metadata = await client.GetMetadataAsync(vin);
        var identifier = Str(metadata, "Identifier") ?? Str(metadata, "identifier")
            ?? throw new InvalidOperationException(
                "No data-request identifier for VIN - is a continuous 15-min request enabled in the portal?");
        Console.WriteLine($"EUDA: got identifier {identifier}");

        var datasets = (await client.ListDatasetsAsync(vin, identifier))
            .Where(d => !(Str(d, "name") ?? "").EndsWith(EudaClient.NoContentSuffix, StringComparison.Ordinal))
            .ToList();
        if (datasets.Count == 0)
            throw new InvalidOperationException("No datasets available yet for this vehicle");
        var latest = datasets.MaxBy(d => Str(d, "createdOn") ?? "")!;
        var latestName = Str(latest, "name")!;
        Console.WriteLine($"EUDA: {datasets.Count} dataset(s) available, downloading latest: {latestName}");

        var payload = await client.DownloadDatasetAsync(vin, identifier, latestName);
        mileage = MileageExtractor.Extract(payload);
        Console.WriteLine($"EUDA: mileage={mileage} km");
    }

    using (var notion = new NotionClient(notionToken, databaseId, property, notionVersion))
        await notion.UpdateAsync(mileage);

    Console.WriteLine($"=== OK: mileage={mileage}, took {stopwatch.Elapsed.TotalSeconds:F1}s ===");
    if (Environment.GetEnvironmentVariable("GITHUB_STEP_SUMMARY") is { Length: > 0 } summary)
        await File.AppendAllTextAsync(summary, $"### ✅ Przebieg zaktualizowany: **{mileage} km**\n");
    return 0;
}
catch (Exception e)
{
    Console.Error.WriteLine($"=== FAILED after {stopwatch.Elapsed.TotalSeconds:F1}s: {e.Message} ===");
    Console.Error.WriteLine(e);
    if (Environment.GetEnvironmentVariable("GITHUB_STEP_SUMMARY") is { Length: > 0 } summary)
        await File.AppendAllTextAsync(summary, $"### ❌ Błąd: {e.Message}\n");
    return 1;
}
