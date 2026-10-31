# SeatConnection

Przycisk „Aktualizuj przebieg” w Notion → GitHub Actions loguje się Playwrightem do portalu
[EU Data Act](https://eu-data-act.drivesomethinggreater.com/), pobiera najnowszy pakiet danych auta,
odczytuje przebieg i wpisuje go do bazy Notion. Dodatkowo uruchamia się automatycznie raz dziennie.

```
Notion (embed) ──► docs/index.html (GitHub Pages) ──► workflow_dispatch
                                                          │
                     Notion API ◄── .NET 8 + Playwright ◄─┘ (GitHub Actions)
```

## Struktura

| Ścieżka | Opis |
|---|---|
| `src/SeatMileage/EudaClient.cs` | Logowanie przez przeglądarkę (VW Identity) i wywołania API portalu |
| `src/SeatMileage/MileageExtractor.cs` | Wyciąganie przebiegu z JSON-a (odrzuca wartości-sentinele) |
| `src/SeatMileage/NotionClient.cs` | Aktualizacja wszystkich wierszy bazy Notion |
| `.github/workflows/update-mileage.yml` | Workflow uruchamiany przyciskiem i cronem (04:00 UTC) |
| `docs/index.html` | Strona z przyciskiem do osadzenia w Notion |

## Konfiguracja

1. **Portal EU Data Act**: zaloguj się na https://eu-data-act.drivesomethinggreater.com/, kliknij „Allow”,
   podepnij auto i włącz ciągłe żądanie danych (co 15 min).
2. **Repo publiczne**: Settings → General → Danger Zone → Change visibility → Public.
3. **Sekrety**: Settings → Secrets and variables → Actions → New repository secret:
   `CUPRA_USER`, `CUPRA_PASS`, `VIN`, `NOTION_TOKEN`, `DATABASE_ID`, `PROPERTY`.
   Opcjonalnie `NOTION_VERSION` (domyślnie `2022-06-28`).
4. **GitHub Pages**: Settings → Pages → Source: *Deploy from a branch* → `main` / `/docs`.
   Strona będzie pod adresem https://wolejniczak.github.io/SeatConnection/.
5. **Token do przycisku**: Settings (konta) → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token. Repository access: *Only select repositories* →
   `SeatConnection`; Permissions → Repository → **Actions: Read and write**.
6. **Notion**: wpisz `/embed` i wklej adres strony z tokenem po `#`:
   `https://wolejniczak.github.io/SeatConnection/#token=github_pat_...`
   Część po `#` nie jest wysyłana do żadnego serwera. Token widzi każdy, kto ma dostęp do tej strony
   w Notion, więc nie udostępniaj jej publicznie.

## Uruchomienie lokalne

```powershell
cd src/SeatMileage
dotnet build
powershell -ExecutionPolicy Bypass -File bin/Debug/net8.0/playwright.ps1 install chromium   # jednorazowo

$env:CUPRA_USER="..."; $env:CUPRA_PASS="..."; $env:VIN="..."
$env:NOTION_TOKEN="..."; $env:DATABASE_ID="..."; $env:PROPERTY="..."
$env:HEADED="1"   # widoczne okno przeglądarki, przydatne do debugowania logowania
dotnet run
```
