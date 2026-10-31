using System.IO.Compression;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.Playwright;

namespace SeatMileage;

public sealed class ApiException(string message) : Exception(message);

public sealed class AuthException(string message) : Exception(message);

/// <summary>Browser login + data-delivery client for the VW Group EU Data Act portal.</summary>
public sealed class EudaClient : IAsyncDisposable
{
    // --- EU Data Act portal constants (undocumented, reverse-engineered) ------
    public const string BaseUrl = "https://eu-data-act.drivesomethinggreater.com";
    private const string OidcAuthorizeUrl = "https://identity.vwgroup.io/oidc/v1/authorize";
    private const string OidcScope = "openid cars profile";
    private const string OidcRedirectUri = BaseUrl + "/login";
    private const string SeatClientId = "f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com";
    private const string SeatState = "SEAT";
    private const string Country = "pl";
    private const string Language = "pl";

    private const string MetadataPath = "/proxy_api/euda-apim/datarequest/vehicles/{0}/metadata/partial";
    private const string ListPath = "/proxy_api/euda-apim/datadelivery/vehicles/{0}/{1}/list";
    private const string DownloadPath = "/proxy_api/euda-apim/datadelivery/vehicles/{0}/{1}/download";
    public const string NoContentSuffix = "_no_content_found.zip";

    private const string UserAgent =
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

    private static readonly string PortalHost = new Uri(BaseUrl).Host;
    private static readonly TimeSpan LoginTimeout = TimeSpan.FromSeconds(60);

    private readonly IPlaywright _playwright;
    private readonly IBrowser _browser;
    private readonly IBrowserContext _context;

    private EudaClient(IPlaywright playwright, IBrowser browser, IBrowserContext context)
    {
        _playwright = playwright;
        _browser = browser;
        _context = context;
    }

    public static async Task<EudaClient> CreateAsync(bool headed)
    {
        var playwright = await Playwright.CreateAsync();
        var browser = await playwright.Chromium.LaunchAsync(new() { Headless = !headed });
        var context = await browser.NewContextAsync(new()
        {
            UserAgent = UserAgent,
            Locale = "pl-PL",
            AcceptDownloads = false,
        });
        return new EudaClient(playwright, browser, context);
    }

    private static string AuthorizeUrl()
    {
        var query = new Dictionary<string, string>
        {
            ["client_id"] = SeatClientId,
            ["response_type"] = "code",
            ["scope"] = OidcScope,
            ["state"] = $"{Country}__{Language}__{SeatState}",
            ["redirect_uri"] = OidcRedirectUri,
            ["prompt"] = "login",
        };
        return OidcAuthorizeUrl + "?" + string.Join("&",
            query.Select(kv => $"{Uri.EscapeDataString(kv.Key)}={Uri.EscapeDataString(kv.Value)}"));
    }

    public async Task LoginAsync(string email, string password)
    {
        Console.WriteLine("EUDA login: starting");
        var page = await _context.NewPageAsync();
        page.SetDefaultTimeout((float)LoginTimeout.TotalMilliseconds);

        try
        {
            await page.GotoAsync(BaseUrl + "/");
        }
        catch (PlaywrightException)
        {
            // priming request, failure is non-fatal
        }

        await page.GotoAsync(AuthorizeUrl());

        // VW Identity runs on Auth0 Universal Login (/u/login): email and password
        // usually sit on one form, but the identifier-first variant (password on a
        // second screen) is handled too.
        var emailInput = page.GetByLabel("Adres e-mail")
            .Or(page.Locator("input[name='username'], input[name='email']")).First;
        var passwordInput = page.GetByLabel("Hasło")
            .Or(page.Locator("input[name='password']")).First;
        var continueButton = page.GetByRole(AriaRole.Button, new() { Name = "Kontynuuj" })
            .Or(page.Locator("button[type='submit'][name='action']")).First;

        await emailInput.FillAsync(email);
        if (!await passwordInput.IsVisibleAsync())
        {
            await continueButton.ClickAsync();
            Console.WriteLine("EUDA login: identifier step submitted");
            try
            {
                await passwordInput.WaitForAsync(new() { State = WaitForSelectorState.Visible });
            }
            catch (TimeoutException)
            {
                throw new AuthException(await DiagnosePageAsync(page) + " (password form not shown - check email)");
            }
        }
        await passwordInput.FillAsync(password);
        await continueButton.ClickAsync();
        Console.WriteLine("EUDA login: credentials submitted");

        // Wait until we land on the portal, or the login page shows an error / consent screen.
        var deadline = DateTime.UtcNow + LoginTimeout;
        while (true)
        {
            var uri = new Uri(page.Url);
            if (uri.Host == PortalHost && !uri.AbsolutePath.StartsWith("/login", StringComparison.Ordinal))
                break;
            if (page.Url.Contains("/consent") || uri.Query.Contains("error=")
                || await page.Locator(LoginErrorSelector).First.IsVisibleAsync())
                throw new AuthException(await DiagnosePageAsync(page));
            if (DateTime.UtcNow > deadline)
            {
                // The portal may stay on /login after the code exchange - accept it.
                if (uri.Host == PortalHost)
                    break;
                throw new AuthException(await DiagnosePageAsync(page));
            }
            await Task.Delay(500);
        }
        await page.WaitForLoadStateAsync(LoadState.NetworkIdle);

        await page.CloseAsync();
        Console.WriteLine("EUDA login: OK");
    }

    // Auth0 Universal Login error elements (field-level and banner).
    private const string LoginErrorSelector =
        "[id^='error-element']:visible, .ulp-input-error-message:visible, #prompt-alert:visible, [data-error-code]:visible";

    private static async Task<string> DiagnosePageAsync(IPage page)
    {
        var errors = await page.Locator(LoginErrorSelector).AllInnerTextsAsync();
        var shown = string.Join(" ", errors.Select(e => e.Trim()).Where(e => e.Length > 0));
        var diagnosis = DiagnoseLoginFailure(page.Url, await page.ContentAsync());
        return shown.Length > 0 ? $"{diagnosis} (page says: {shown})" : diagnosis;
    }

    /// <summary>
    /// Map a failed-login landing page to an actionable message.
    /// Ported from ioBroker.vw-connect's euDataAct.js diagnoseLoginFailure.
    /// </summary>
    private static string DiagnoseLoginFailure(string landingUrl, string landingHtml)
    {
        if (landingUrl.Contains("/consent") || landingHtml.Contains("consent-screen"))
            return "EU Data Act consent screen is blocking the login - open " +
                   $"{BaseUrl}/ in a browser, log in with the same credentials, " +
                   "click Allow, then finish portal setup (vehicle linking + " +
                   "continuous 15-min data request).";

        var m = Regex.Match(new Uri(landingUrl).Query, "error=([^&]+)");
        var text = m.Success ? Uri.UnescapeDataString(m.Groups[1].Value) : ExtractPageError(landingHtml) ?? "";
        if (Regex.IsMatch(text, "password_invalid", RegexOptions.IgnoreCase))
            return "Login failed: password incorrect.";
        if (Regex.IsMatch(text, "throttle|rate_limit|too_many", RegexOptions.IgnoreCase))
            return "Login failed: too many attempts, account throttled - wait ~30 min.";
        if (Regex.IsMatch(text, "account_disabled|locked|blocked", RegexOptions.IgnoreCase))
            return "Login failed: VW account is locked/disabled - reset password at identity.vwgroup.io.";
        if (text.Length > 0)
            return $"Login failed: {text}";
        return $"Login failed (no error code reported). Landing URL: {landingUrl.Split('?')[0]}";
    }

    private static string? ExtractPageError(string html)
    {
        var m = Regex.Match(html, "\"error(?:Code)?\"\\s*:\\s*\"([^\"]+)\"");
        return m.Success ? m.Groups[1].Value : null;
    }

    private async Task<IAPIResponse> GetAsync(string path, Dictionary<string, string>? headers = null)
    {
        var url = BaseUrl + path;
        var resp = await _context.APIRequest.GetAsync(url, new() { Headers = headers });
        if (resp.Status >= 400)
        {
            var text = await resp.TextAsync();
            throw new ApiException($"GET {path} -> HTTP {resp.Status}: {text[..Math.Min(300, text.Length)]}");
        }
        return resp;
    }

    public async Task<JsonElement> GetMetadataAsync(string vin)
    {
        var resp = await GetAsync(string.Format(MetadataPath, vin));
        return JsonDocument.Parse(await resp.TextAsync()).RootElement;
    }

    public async Task<List<JsonElement>> ListDatasetsAsync(string vin, string identifier)
    {
        var resp = await GetAsync(string.Format(ListPath, vin, identifier),
            new() { ["type"] = "partial" });
        var root = JsonDocument.Parse(await resp.TextAsync()).RootElement;
        if (root.ValueKind == JsonValueKind.Array)
            return [.. root.EnumerateArray()];
        return root.TryGetProperty("files", out var files) ? [.. files.EnumerateArray()] : [];
    }

    public async Task<JsonElement> DownloadDatasetAsync(string vin, string identifier, string name)
    {
        if (name.EndsWith(NoContentSuffix, StringComparison.Ordinal))
            throw new ApiException($"{name} contains no content");
        var resp = await GetAsync(string.Format(DownloadPath, vin, identifier),
            new() { ["filename"] = name, ["type"] = "partial" });

        using var zip = new ZipArchive(new MemoryStream(await resp.BodyAsync()), ZipArchiveMode.Read);
        var member = zip.Entries.FirstOrDefault(e => e.FullName.EndsWith(".json", StringComparison.OrdinalIgnoreCase))
                     ?? throw new ApiException($"No JSON inside {name}");
        await using var stream = member.Open();
        using var doc = await JsonDocument.ParseAsync(stream);
        return doc.RootElement.Clone();
    }

    public async ValueTask DisposeAsync()
    {
        await _context.CloseAsync();
        await _browser.CloseAsync();
        _playwright.Dispose();
    }
}
