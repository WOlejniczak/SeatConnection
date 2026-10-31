using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace SeatMileage;

public sealed class NotionClient(string token, string databaseId, string property, string notionVersion) : IDisposable
{
    private readonly HttpClient _http = CreateHttp(token, notionVersion);

    private static HttpClient CreateHttp(string token, string notionVersion)
    {
        var http = new HttpClient { BaseAddress = new Uri("https://api.notion.com/v1/") };
        http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
        http.DefaultRequestHeaders.Add("Notion-Version", notionVersion);
        return http;
    }

    public async Task<List<string>> GetAllPageIdsAsync()
    {
        var pageIds = new List<string>();
        string? startCursor = null;
        bool hasMore = true;

        while (hasMore)
        {
            var payload = new JsonObject { ["page_size"] = 100 };
            if (startCursor is not null)
                payload["start_cursor"] = startCursor;

            using var r = await _http.PostAsJsonAsync($"databases/{databaseId}/query", payload);
            var text = await r.Content.ReadAsStringAsync();
            if (!r.IsSuccessStatusCode)
                throw new InvalidOperationException($"Failed to query database: {text}");

            using var doc = JsonDocument.Parse(text);
            var root = doc.RootElement;
            foreach (var page in root.GetProperty("results").EnumerateArray())
                pageIds.Add(page.GetProperty("id").GetString()!);
            hasMore = root.TryGetProperty("has_more", out var hm) && hm.GetBoolean();
            startCursor = root.TryGetProperty("next_cursor", out var nc) && nc.ValueKind == JsonValueKind.String
                ? nc.GetString()
                : null;
        }

        return pageIds;
    }

    public async Task UpdateAsync(int value)
    {
        var pageIds = await GetAllPageIdsAsync();
        if (pageIds.Count == 0)
            throw new InvalidOperationException("Database is empty - no rows to update");
        Console.WriteLine($"Notion: updating {pageIds.Count} row(s) to mileage={value}");

        var failed = new List<object>();
        foreach (var pageId in pageIds)
        {
            var body = new JsonObject
            {
                ["properties"] = new JsonObject { [property] = new JsonObject { ["number"] = value } },
            };
            using var r = await _http.PatchAsJsonAsync($"pages/{pageId}", body);
            if (!r.IsSuccessStatusCode)
                failed.Add(new { page_id = pageId, error = await r.Content.ReadAsStringAsync() });
        }

        if (failed.Count > 0)
            throw new InvalidOperationException($"Failed to update {failed.Count} rows: {JsonSerializer.Serialize(failed)}");
    }

    public void Dispose() => _http.Dispose();
}
