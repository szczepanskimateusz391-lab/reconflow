# ReconFlow

ReconFlow to lokalna aplikacja demonstracyjna do uzgadniania zamówień, płatności, dokumentów sprzedaży i zwrotów w e-commerce. Importuje cztery pliki CSV, oblicza wynik na wybrany moment i pokazuje sprawy wymagające sprawdzenia wraz z wyliczeniem oraz odwołaniem do wierszy źródłowych.

**Wszystkie dołączone dane, decyzje na zrzutach i wyniki demonstracyjne są syntetyczne.** Projekt nie jest przygotowany do pracy na danych produkcyjnych.

## Problem i działanie

Dane dotyczące jednego zamówienia często znajdują się w kilku eksportach. Samo znalezienie powiązanej płatności nie wystarcza: może być częściowa, oczekująca, w innej walucie albo wykonana już po dacie analizy. ReconFlow rozdziela identyfikację powiązania od oceny kwoty i statusu transakcji.

- Import sprawdza schematy CSV i zapisuje odrzucone wiersze z przyczyną. Identyczny rekord pomija, a zmieniony rekord o tym samym kluczu zgłasza jako konflikt bez nadpisania.
- Uzgadnianie używa identyfikatora zamówienia razem z systemem źródłowym. Bez jednoznacznego odwołania pokazuje kandydatów do ręcznej decyzji; wynik reguł nie jest procentem pewności.
- Podsumowanie oddziela wykryte różnice kwotowe, kwoty wymagające wyjaśnienia i wypłaty wymagające osobnego procesu. Waluty są prezentowane osobno, bez automatycznego przewalutowania.
- Kolejka łączy powiązane ustalenia w przypadki operacyjne. Można zapisać powiązanie, status i komentarz, przejrzeć historię oraz wyeksportować widoczne przypadki do CSV.

Na przykład A-101 ma wartość `150,00 PLN` i zakończoną wpłatę `100,00 PLN`, więc pozostaje `50,00 PLN` niedopłaty. Status „Rozwiązany” zamyka obsługę sprawy, ale sam nie usuwa tej różnicy ani nie potwierdza odzyskania pieniędzy.

## Zrzuty

Zrzuty pochodzą z osobnej bazy PostgreSQL z danymi syntetycznymi; pokazują rzeczywisty interfejs, nie atrapę. Nie są dowodem uruchomienia całego zestawu Playwright przeciw Docker Compose.

| Widok | Zrzut |
| --- | --- |
| Przegląd wyników i kwoty według waluty | [Podsumowanie](docs/portfolio/screenshots/reconflow-podsumowanie.png) |
| Kolejka przypadków | [Problemy](docs/portfolio/screenshots/reconflow-kolejka.png) |
| A-101: wyliczenie, źródła i historia decyzji | [Szczegóły A-101](docs/portfolio/screenshots/reconflow-niedoplata-50.png) |

## Uruchomienie przez Docker

Wymagane są Docker Desktop z działającym silnikiem i wtyczką Compose. W katalogu głównym projektu uruchom:

```bash
docker compose up --build
```

Po osiągnięciu stanu `healthy` otwórz [aplikację](http://127.0.0.1:8080) lub [dokumentację API](http://127.0.0.1:8000/docs). Compose uruchamia PostgreSQL, migracje Alembic, FastAPI i frontend nginx. Porty są domyślnie przypięte do `127.0.0.1`; baza używa trwałego wolumenu. `docker compose stop` zatrzymuje usługi bez usuwania danych. Nie używaj `down -v`, jeśli chcesz zachować wolumen.

Demonstracyjne hasło bazy zapisane w `docker-compose.yml` służy wyłącznie do lokalnego uruchomienia. Porty można zmienić przez `BACKEND_PORT` i `FRONTEND_PORT`; tolerancję kwot, termin dokumentu i wymagane typy dokumentów konfiguruje się w Compose. Publiczne wdrożenie wymagałoby osobnej konfiguracji bezpieczeństwa.

### Krótka ścieżka przez demo

1. W **Importach** wgraj kolejno `demo-data/orders.csv`, `payments.csv`, `documents.csv` i `returns.csv`. Liczniki pokażą dodane, pominięte i odrzucone rekordy. Ponowny import tego samego pliku nie tworzy duplikatów.
2. W **Przeglądzie** uruchom uzgadnianie dla historycznego momentu danych demo: `2025-02-15 12:00 UTC` (`13:00` w Warszawie). Pole formularza pokazuje lokalny czas **następnej** analizy, a podsumowanie datę **ostatniego** wyniku.
3. W **Problemach** otwórz A-101. Sprawdź `150,00 − 100,00 = 50,00 PLN`, termin, dokument i wiersze źródłowe. Jeśli sprawa ma już status „Rozwiązany”, wybierz filtr obejmujący zamknięte przypadki.
4. Na świeżej bazie demonstracyjnej zapisz status i komentarz. Po ponownej analizie historia decyzji zostaje, a niedopłata pozostaje w wyniku finansowym, dopóki dane płatności jej nie usuną. Eksport CSV obejmuje widoczne po filtrach przypadki; jeden wiersz oznacza jeden przypadek operacyjny.

Przykładowy konflikt identyfikatora bez nadpisania można sprawdzić plikiem `demo-data/conflicting-order.csv`. Schematy i znaczenie pól opisuje [kontrakt CSV](docs/csv-schemas.md), a ręcznie ustalone oczekiwania — [przypadki testowe](docs/manual-cases.md).

## Testy i potwierdzony zakres

Zależności są przypięte w `backend/requirements*.lock` i `frontend/pnpm-lock.yaml`. Dla lokalnych testów na Windows potrzebne są Python 3.12, Node.js z Corepack/pnpm oraz PostgreSQL 18 z narzędziami w `PATH` lub ścieżką `RECONFLOW_POSTGRES_BIN`. Testy zmieniają wyłącznie dedykowane bazy; nie uruchamiaj ich przeciw bazie z własnymi decyzjami.

```powershell
# Z katalogu głównego projektu
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider
.\tests\run-postgres-integration.ps1

# Frontend
cd ..\frontend
corepack enable
corepack prepare pnpm@11.19.0 --activate
pnpm install --frozen-lockfile
pnpm run build
pnpm exec playwright install chromium
pnpm run test:e2e:standalone
```

`test:e2e:standalone` uruchamia osobny klaster PostgreSQL, API i Vite. Przed importem test sprawdza znacznik instancji testowej. Zestaw integracyjny PostgreSQL uruchamia osobny klaster i sprawdza również równoczesne próby przypisania jednej transakcji. Szczegóły środowisk i poleceń są w [raporcie audytu](docs/final-audit.md).

| Kontrola | Rzeczywisty wynik | Zakres |
| --- | --- | --- |
| Backend, 18.09.2026 | `30 passed, 4 skipped` | Pominięte testy wymagały dedykowanego PostgreSQL i przeszły osobno. |
| Integracja PostgreSQL, 18.09.2026 | `4 passed` | Import, `NUMERIC`, granice dat i konflikt równoczesnych przypisań. |
| Pełny lokalny przebieg Playwright, 21.09.2026 | `5 passed` | Osobna baza PostgreSQL, nie Compose. Ten historyczny przebieg poprzedza nowy test importu. |
| Izolowany Docker Compose, 20.09.2026 | PASS | Build, migracje, healthchecki, cztery importy, analiza, decyzja i trwałość po restarcie. |
| Komunikat ponownego importu, 26.09.2026 | `1 passed` | Jeden wybrany test Playwright na osobnym Compose: `skipped_count=0` bez komunikatu i `skipped_count=1` z komunikatem. [Pierwszy](docs/evidence/import-skipped-first.png) i [drugi](docs/evidence/import-skipped-repeat.png) zrzut. |
| Build frontendu kopii publikacyjnej, 26.09.2026 | PASS | `docker compose -p reconflow-public-prep build frontend`; TypeScript i Vite zakończyły się bez błędu. |
| **Pełny zestaw Playwright przeciw Compose** | **NOT RUN** | Nie należy utożsamiać go z pojedynczym testem importu ani z lokalnym przebiegiem na PostgreSQL. |

Generator 10 000 syntetycznych zamówień ma stały seed i niezależne pliki oczekiwanych powiązań oraz alertów. [Wyniki pomiaru](docs/benchmark-results.json) dotyczą kontrolowanego zbioru i harnessu SQLite, **nie** wydajności PostgreSQL ani danych rzeczywistych. Nie stanowią prognozy odzyskanych środków czy oszczędności czasu.

## Struktura i ograniczenia

- `backend/`: FastAPI, reguły uzgadniania, SQLAlchemy, migracje Alembic i Pytest;
- `frontend/`: React, TypeScript, Vite, Tailwind CSS i testy Playwright;
- `demo-data/`: syntetyczne pliki demonstracyjne;
- `docs/`: schematy CSV, przypadki, audyt i zrzuty;
- `scripts/`: generator danych i pomiar.

To MVP dla jednej firmy demonstracyjnej, bez logowania, ról, integracji bankowych lub marketplace, automatycznego przewalutowania, księgowania i działań w zewnętrznych systemach. Zbiorcze wypłaty marketplace, prowizje i konflikty walut wymagają osobnego procesu. Przed użyciem produkcyjnym potrzebne byłyby m.in. kontrola dostępu, ochrona danych, wdrożeniowa konfiguracja sekretów i walidacja na rzeczywistych danych.

Repozytorium nie określa obecnie licencji na kod aplikacji. Zależności zachowują własne licencje i informacje o autorach; ich nazwy oraz wersje znajdują się w plikach zależności i blokad.
