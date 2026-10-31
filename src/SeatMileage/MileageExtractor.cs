using System.Globalization;
using System.Text.Json;

namespace SeatMileage;

public static class MileageExtractor
{
    // Sentinel values the portal emits when a data point has no current reading.
    // Without filtering these leak through as e.g. "mileage: 4294967295".
    private static readonly HashSet<long> GenericIntSentinels = [65535, 2147483647, 4294967295];

    /// <summary>
    /// Extract mileage from the raw dataset's flat <c>Data</c> array.
    ///
    /// The portal bundles several report snapshots per 15-min dataset, so the
    /// same dataFieldName (e.g. "mileage.value") can appear multiple times with
    /// different values, and some entries are protocol sentinels rather than
    /// real readings. Mileage is monotonically non-decreasing, so among usable
    /// duplicates the largest value is the freshest. Logic ported from
    /// ioBroker.vw-connect's euDataAct.js.
    /// </summary>
    public static int Extract(JsonElement dataset)
    {
        double? best = null;
        if (dataset.TryGetProperty("Data", out var data) && data.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in data.EnumerateArray())
            {
                var field = GetString(item, "dataFieldName") ?? GetString(item, "key");
                if (field is not ("mileage.value" or "mileage"))
                    continue;
                if (!TryGetNumber(item, "value", out var val))
                    continue;
                if (GenericIntSentinels.Contains((long)val))
                    continue;
                if (best is null || val > best)
                    best = val;
            }
        }
        if (best is null)
            throw new InvalidOperationException("No usable mileage reading in dataset (only sentinels or field missing)");
        return (int)best.Value;
    }

    private static string? GetString(JsonElement item, string name) =>
        item.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

    private static bool TryGetNumber(JsonElement item, string name, out double value)
    {
        value = 0;
        if (!item.TryGetProperty(name, out var v))
            return false;
        return v.ValueKind switch
        {
            JsonValueKind.Number => v.TryGetDouble(out value),
            JsonValueKind.String => double.TryParse(v.GetString(), NumberStyles.Float, CultureInfo.InvariantCulture, out value),
            _ => false,
        };
    }
}
