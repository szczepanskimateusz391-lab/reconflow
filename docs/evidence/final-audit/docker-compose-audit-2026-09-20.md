# Izolowany audyt Docker Compose — 20 września 2026

Poniższe wyniki pochodzą z rzeczywistych poleceń wykonanych na tym hoście. Nie są wynikiem sprawdzenia samego YAML. Żadne polecenie nie używało `down -v`, projektu `reconflow` ani jego wolumenu. Przed startem `docker ps -a` i `docker volume ls` nie zwracały żadnych wpisów.

## Środowisko i izolacja

- Docker Client `29.8.0`, Server `29.8.0` (`Docker Desktop 4.91.0`, kontekst `desktop-linux`), Compose `v5.5.1`.
- CLI uruchomiono z instalacji Docker Desktop: `%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin\docker.exe` (w PowerShell: `Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'`). Samo polecenie `docker` nie było dostępne w użytej powłoce; bezpośrednia ścieżka do CLI działała.
- `docker compose -p reconflow-audit config` pokazał sieć `reconflow-audit_default`, wolumen `reconflow-audit_reconflow_pgdata`, backend `127.0.0.1:18000:8000`, frontend `127.0.0.1:18080:80` i bazę bez portu hosta.
- `docker volume inspect reconflow-audit_reconflow_pgdata` potwierdził etykietę `com.docker.compose.project=reconflow-audit` przed i po restarcie.

## Polecenia i wyniki

Z katalogu głównego projektu wykonano (w sesji audytu `$docker` wskazywał plik z instalacji użytkownika; poniżej zapisano przenośny odpowiednik):

```powershell
$env:BACKEND_PORT = '18000'
$env:FRONTEND_PORT = '18080'
$docker = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'
& $docker version
& $docker compose version
& $docker compose -p reconflow-audit config
& $docker compose -p reconflow-audit up --build -d
& $docker compose -p reconflow-audit ps
& $docker compose -p reconflow-audit logs backend --tail 50
& $docker compose -p reconflow-audit exec -T backend alembic current
Invoke-RestMethod http://127.0.0.1:18080/api/health
```

Build zakończył się kodem `0`: obrazy `reconflow-audit-backend:latest` (`sha256:22100959b245d6b04c2a54fa0c9a24cd5ba3cfb1cde609a21c773ae431899b38`) i `reconflow-audit-frontend:latest` (`sha256:e784be7c40cec164e1fb5e91a057c3dbf7f0a0174d1b565aa0ce2fa414fa1bdf`) zostały zbudowane; frontend wewnątrz builda wykonał `tsc -b && vite build` (`28 modules transformed`, JS `181.01 kB`). Start utworzył tylko kontenery z prefiksem `reconflow-audit-` i audytowy wolumen. `compose ps` zwrócił trzy usługi `healthy`. Log backendu pokazał `Running upgrade -> 0001` i `Running upgrade 0001 -> 0002`; `alembic current` zwrócił `0002 (head)`. PostgreSQL w kontenerze: `17.2`. Endpoint przez proxy frontendu zwrócił `status=ok`, `api=available`, `database=available`.

Przed importem zapytanie SQL o liczby `orders`, `payments`, `documents`, `returns`, `import_batches` zwróciło `0|0|0|0|0`. Cztery pliki zaimportowano przez `POST http://127.0.0.1:18080/api/imports/{dataset}` z multipart `file` (`Invoke-RestMethod -Form @{file=Get-Item ...}`):

```powershell
foreach ($dataset in @('orders', 'payments', 'documents', 'returns')) {
    $file = Get-Item -LiteralPath (Join-Path '.\demo-data' ($dataset + '.csv'))
    Invoke-RestMethod -Method Post -Uri ('http://127.0.0.1:18080/api/imports/' + $dataset) -Form @{file = $file}
}
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:18080/api/reconciliations' `
    -ContentType 'application/json' -Body '{"analysis_at":"2025-02-15T12:00:00Z"}'
```

| Zbiór | Batch | Dodane | Pominięte | Konflikty | Odrzucone |
|---|---:|---:|---:|---:|---:|
| `orders.csv` | 1 | 15 | 0 | 0 | 0 |
| `payments.csv` | 2 | 20 | 0 | 0 | 0 |
| `documents.csv` | 3 | 18 | 0 | 0 | 0 |
| `returns.csv` | 4 | 3 | 0 | 0 | 0 |

`POST /api/reconciliations` przez `18080` z `{"analysis_at":"2025-02-15T12:00:00Z"}` zwrócił przebieg `id=1`, `status=completed`, `automatic_reference_links=17`. Szczegóły `GET /api/cases/order-2` pokazały syntetyczne A-101: zamówienie `150.00 PLN`, zakończona płatność `P-101` `100.00 PLN`, termin `2025-01-10`, brakujące `50.00 PLN` oraz pliki i wiersze źródłowe. Podsumowanie przed decyzją: `active_alerts=13`, `detected_cases=13`, PLN `amount_differences=165.00`, `requires_explanation=1304.00`, `separate_process=950.00`.

`POST /api/cases/order-2/status` przez `18080` z `status=resolved` i komentarzem „Izolowany test Docker 2026-09-20: sprawę przejrzano; niedopłata 50 PLN pozostaje w danych. Nie potwierdzono odzyskania środków.” zwrócił HTTP 200. Nie podano `confirmed_effect_amount`. Następnie API zwróciło `active_alerts=12`, `detected_cases=13`, PLN `amount_differences=165.00`, `confirmed_user_effect=0.00`; historia A-101 zawierała `detected` i dokładnie jeden `status_changed`.

```powershell
$body = @{
    status = 'resolved'
    comment = 'Izolowany test Docker 2026-09-20: sprawę przejrzano; niedopłata 50 PLN pozostaje w danych. Nie potwierdzono odzyskania środków.'
} | ConvertTo-Json -Compress
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:18080/api/cases/order-2/status' `
    -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
```

Wykonano rzeczywisty restart, bez usuwania wolumenu:

```powershell
& $docker compose -p reconflow-audit stop
& $docker compose -p reconflow-audit start
& $docker compose -p reconflow-audit ps
& $docker compose -p reconflow-audit exec -T backend alembic current
```

Po restarcie `compose ps` ponownie pokazał trzy usługi `healthy`, `alembic current` nadal `0002 (head)`, a `/api/health` przez frontend zwrócił `ok/available`. Odczyty przez frontendowe `/api` wykazały: 4 importy, 1 przebieg, 1 sprawę w filtrze `resolved`, A-101 nadal `resolved` i `50.00 PLN`, historia 2 wpisy (w tym dokładnie jeden komentarz testowy), `active_alerts=12`, `detected_cases=13`, różnice PLN `165.00`, potwierdzony efekt `0.00`. SQL w bazie po restarcie: `15|20|18|3|4|15` dla `orders|payments|documents|returns|import_batches|alert_history`. Log backendu po restarcie pokazał start bez ponownego wykonywania migracji.

Po restarcie wykonano dodatkową kontrolę w przeglądarce pod `http://127.0.0.1:18080/`: **Przegląd** pokazał zielony wskaźnik „API i baza połączone”, `12 aktywnych` i PLN `165,00 zł` różnic; **Importy** pokazały cztery ukończone batche i liczby 15/20/18/3; **Problemy** pokazały `12 wyników` dla Aktywnych i `1 wynik` po filtrze Rozwiązane. W szczegółach A-101 widoczne były `150,00 − 100,00 = 50,00 PLN`, P-101, dokument D-101, terminy, źródła i historia decyzji. Z poziomu formularza ponownie zapisano **identyczny** status i komentarz; UI pokazał potwierdzenie zapisu. Ponowny odczyt API potwierdził, że historia nadal ma 2 wpisy, w tym dokładnie 1 `status_changed`; status `resolved`, niedopłata `50.00`, aktywne `12` i różnice PLN `165.00` nie zmieniły się.

Ta kontrola w przeglądarce była ręcznym smoke testem kontenerowego uruchomienia, nie ponownym uruchomieniem pełnego zestawu Playwright. Osobny automatyczny test Chromium jest podsumowany w [audycie](../../final-audit.md); surowy log pozostał tylko w oryginalnym repozytorium, nie w kopii publikacyjnej. Kontenery audytowe pozostawiono uruchomione, a wolumen zachowano.
