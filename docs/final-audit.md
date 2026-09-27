# Audyt ReconFlow

Ten raport rozdziela wyniki historycznych przebiegów od kontroli wykonanej 26 września 2026 r. Wszystkie dane testowe były syntetyczne. Mutujące testy działały na odrębnych bazach lub wolumenach; nie używały bazy z decyzjami użytkownika. `PASS` oznacza wykonane sprawdzenie zgodne z oczekiwaniem, `NOT RUN` — sprawdzenie niewykonane. Nie utożsamiamy symulowanej awarii z rzeczywistym zatrzymaniem usługi.

## Wyniki i środowiska

| Data i środowisko | Wykonane sprawdzenie | Wynik | Dowód |
| --- | --- | --- | --- |
| 18.09.2026, Python 3.12, Pytest | Testy backendu na izolowanej bazie SQLite | **PASS:** `30 passed, 4 skipped`; pominięte cztery wymagają PostgreSQL | [`backend/tests`](../backend/tests) |
| 18.09.2026, PostgreSQL 18.4 | Osobny klaster: import, precyzja `NUMERIC`, granice dat i konkurencyjne przypisania | **PASS:** `4 passed` | [`test_postgres_integration.py`](../backend/tests/test_postgres_integration.py) |
| 21.09.2026, odrębny PostgreSQL, Chromium | Pełny ówczesny lokalny zestaw Playwright, łącznie z testem strefy `Europe/Warsaw` | **PASS:** `5 passed (12,4 s)` | [`main-flow.spec.ts`](../frontend/tests/main-flow.spec.ts) |
| 20.09.2026, Docker 29.8 / Compose 5.5.1 | Build, migracje, healthchecki, import, analiza, decyzja i restart z zachowaniem wolumenu | **PASS** | [Zapis kontroli Compose](evidence/final-audit/docker-compose-audit-2026-09-20.md) |
| 26.09.2026, osobny projekt Compose i Chromium | **Jeden** test komunikatu identycznego ponownego importu | **PASS:** `1 passed (5,7 s)` | [Pierwszy import](evidence/import-skipped-first.png), [ponowny import](evidence/import-skipped-repeat.png), test Playwright |
| 26.09.2026, kopia publikacyjna | Build frontendu przez `docker compose -p reconflow-public-prep build frontend` | **PASS:** TypeScript i Vite, 28 modułów | Polecenie builda; bez uruchamiania bazy |
| 26.09.2026, Compose | **Cały aktualny zestaw Playwright** przeciw kontenerom Compose | **NOT RUN** | Pojedynczy test importu powyżej nie jest pełnym zestawem |

Historyczny wynik `5 passed` poprzedza dodanie testu identycznego importu. Po tej zmianie nie uruchomiono ponownie całego lokalnego zestawu. Dzienniki poprzednich przebiegów nie są dołączone do kopii publikacyjnej; tabela wskazuje dostępny kod testów, zrzuty i zachowany zapis kontroli Compose, nie nieistniejące pliki logów.

## Import i identyfikatory

| Oczekiwanie wyznaczone z danych testowych | Zaobserwowany wynik | Status i test |
| --- | --- | --- |
| Cztery poprawne pliki demo: 15 zamówień, 20 płatności, 18 dokumentów, 3 zwroty; bez odrzuceń | Liczniki zgodne | **PASS** — `test_all_demo_files_import_without_rejections` |
| Ponowny import czterech plików: zero nowych rekordów, odpowiednio 15/20/18/3 pominięte | Liczniki zgodne, bez duplikatów | **PASS** — `test_reimport_of_all_four_demo_files_only_skips_existing_records` |
| Zmienione `shop-a/A-100`: konflikt bez nadpisania pierwotnych `100,00 PLN` | `conflict=1`, `rejected=1`; pierwotny rekord bez zmian | **PASS** — `test_changed_record_is_conflict_and_not_overwritten` |
| Brak kolumny, błędna data i kwota: jawny powód, plik i numer wiersza | Odrzucone wiersze i raport CSV zgadzały się z plikiem; błędne dane były w wierszach 3 i 4 | **PASS** — testy walidacji w [`test_importer.py`](../backend/tests/test_importer.py) |
| Ten sam numer z dwóch sklepów nie tworzy jednego zamówienia | `shop-a/A-100=100,00` i `shop-b/A-100=55,00` pozostały osobne | **PASS** — `test_same_identifier_in_two_sources_is_not_merged` |
| Nowy plik z jednym rekordem: `skipped_count=0`, bez komunikatu; drugi import: `skipped_count=1`, komunikat widoczny | API i interfejs zwróciły dokładnie te wyniki | **PASS** — jeden test Playwright `identical reimport explains...` i dwa zrzuty powyżej |

Test PostgreSQL potwierdził dodatkowo, że konflikt istniejącego klucza nie nadpisuje wartości `Decimal('150.10')`. W pierwszym przebiegu ujawnił błąd: `ImportBatch.status` miał `VARCHAR(20)`, zbyt krótkie dla `completed_with_errors`. Model i migrację `0002` poprawiono; cztery testy PostgreSQL przeszły po poprawce.

## Obliczenia, terminy i decyzje

| Oczekiwanie | Zaobserwowany wynik | Status i test |
| --- | --- | --- |
| A-101: `150,00 − 100,00 = 50,00 PLN`; źródła, termin i data oceny widoczne w szczegółach | P-101 jest zakończoną wpłatą 100,00; niedopłata 50,00 pozostaje także po zmianie statusu | **PASS** — `test_underpayment_case_has_auditable_calculation_and_sources`; [zrzut A-101](portfolio/screenshots/reconflow-niedoplata-50.png) |
| A-109: dwie wpłaty po 50,00 rozliczają 100,00; podejrzenie duplikatu nie jest nadpłatą ani zaległością | Suma 100,00; brak alertu nadpłaty i sztucznego terminu | **PASS** — `test_a109_duplicate_is_balanced_and_has_no_overdue_deadline` |
| Płatności częściowe, refundacje, statusy `pending`/`failed` i waluty są traktowane oddzielnie | A-102: 60+140=200; R-103: 40 oczekiwane − 20 zakończone = 20; oczekujące i nieudane nie zwiększają wpłat; PLN nie jest odejmowane od EUR | **PASS** — testy sum, refundacji i konfliktu walut w [`test_reconciliation.py`](../backend/tests/test_reconciliation.py) |
| Przed terminem brak zaległości; płatności i refundacje późniejsze od daty oceny nie rozliczają wcześniejszej analizy | Wyniki przed i po terminie oraz przed i po transakcji zgodne z literalnymi kwotami | **PASS** — `test_pending_before_due_is_not_overdue_and_failed_is_not_paid`, testy `test_future_completed_*` |
| Dwaj równie dobrzy kandydaci nie są automatycznie łączeni; jedna transakcja nie służy dwóm zamówieniom | Brak automatycznego przypisania niejednoznacznej płatności; drugi ręczny zapis dostaje HTTP 409 | **PASS** — `test_ambiguous_candidate_is_not_auto_linked`, `test_payment_cannot_be_assigned_twice` |
| `resolved` zmienia obsługę, nie kwotę; ponowna analiza zachowuje komentarz i nie duplikuje ustalenia | Liczba aktywnych spada, wykryta różnica zostaje; historia po ponownym przebiegu ma tę samą decyzję | **PASS** — `test_case_counters_refresh_without_changing_detected_amount`, `test_rerun_reuses_alerts_and_preserves_manual_decision` |

Osobny scenariusz A-101 sprawdził kolejno analizę **15 stycznia** przed dopłatą, **25 stycznia** po nowej zakończonej wpłacie 50,00 PLN i ponownie **15 stycznia** w tej samej bazie. Wyniki były odpowiednio `50,00`, `0,00`, `50,00 PLN`. Historia wcześniejszego ustalenia i decyzji pozostała, a nowe dane podważające zamkniętą sprawę wywołały sygnał ponownej oceny. Ponownie bieżąca niedopłata ustawiła obsługę na `in_progress` bez drugiej kopii tego alertu. **PASS** — `test_changed_data_preserves_history_and_recalculates_for_each_analysis_date`, `test_new_data_can_challenge_a_preserved_manual_decision` oraz historyczny test Playwright.

Na prawdziwym PostgreSQL dwie równoczesne próby przypisania tej samej płatności do różnych zamówień dały dokładnie jeden zapis i jeden kontrolowany konflikt HTTP 409. W bazie zostały jeden `PaymentAssignment` i jedna `UserDecision`. **PASS** — `test_postgres_concurrent_assignment_has_one_winner_and_controlled_conflict`. To sprawdza transakcje i blokady, których nie zastępuje test SQLite.

## Podsumowanie, kolejka i eksport

- Dla syntetycznego zestawu demo przy ocenie `2025-02-15T12:00:00Z` suma różnic PLN wyniosła `165,00` (`50+20+70+5+15+5`), kwoty do wyjaśnienia `1 304,00`, a wypłaty w osobnym procesie `950,00`. EUR: `0,00` różnicy i `100,00` do wyjaśnienia. Wkłady do sum mają unikalną podstawę, więc ta sama różnica nie jest liczona drugi raz przez powiązany alert. **PASS** — `test_summary_separates_amount_meanings_and_currencies`.
- Wypłata marketplace `950,00 PLN` i nierozpoznana transakcja `999,00 PLN` nie zostały oznaczone automatycznie jako strata. Po zamknięciu A-101 aktywne sprawy zmieniły się z 13 na 12, ale wykryte różnice PLN pozostały `165,00`. **PASS** — test podsumowania i liczników.
- Podsumowanie i kolejka stosują kolejność: priorytet malejąco, termin rosnąco, brak terminu na końcu i stabilny identyfikator przy remisie. Sortowanie kwot wymaga jednej waluty. **PASS** — testy `test_summary_and_queue_share_canonical_priority_order`, `test_amount_sort_requires_single_currency`.
- Eksport respektuje filtry i sortowanie; `row_type=operational_case`, a `finding_ids` wskazuje składowe ustalenia. Test sprawdził identyfikatory, statusy, kwoty, waluty, polskie znaki i brak duplikatów. **PASS** — `test_case_export_matches_filters_and_has_one_row_per_operational_case`.

## Interfejs, awarie i uruchomienie Compose

Test Playwright sprawdził zapis komentarza, odświeżenie liczników, zachowanie decyzji po ponownej analizie, komunikat nieudanego zapisu i brak podwójnego wpisu po ponowieniu. Awarie API, bazy oraz pobrania widoku były **symulowane przez `page.route`**; przywrócenie żądania i „Spróbuj ponownie” odtworzyły widok. Nie zatrzymywano w tym teście zwykłego backendu ani bazy. **PASS** — pierwsze trzy testy w [`main-flow.spec.ts`](../frontend/tests/main-flow.spec.ts).

Rzeczywiste zatrzymanie i ponowne uruchomienie API na odrębnej bazie zachowało status, komentarz i `50,00 PLN` różnicy. Osobny projekt Compose `reconflow-audit` zbudował obrazy, wykonał migrację `0002 (head)` i pokazał trzy usługi `healthy`. Po imporcie czterech plików, uzgodnieniu i zapisie decyzji wykonano `stop/start` **bez** `down -v`; dane i historia pozostały w wolumenie. Przegląd, Importy, Problemy i szczegóły A-101 sprawdzono w przeglądarce. **PASS** — [zapis kontroli Compose](evidence/final-audit/docker-compose-audit-2026-09-20.md). Ten smoke test przeglądarkowy nie był pełnym przebiegiem Playwright przeciw Compose.

Po odkryciu rozbieżności `12:00` w polu i `13:00` w podsumowaniu ustalono, że API zwracało `12:00Z`, czyli prawidłowo `13:00` w Warszawie, a formularz miał niezależną wartość lokalną. Poprawiono synchronizację nieedytowanego pola z ostatnim wynikiem i oznaczono zmianę jako dotyczącą następnej analizy. Regresyjny test w strefie `Europe/Warsaw` przeszedł w lokalnym zestawie `5 passed`. Widoki obejrzano przy 100% powiększenia i szerokościach 1366 oraz 1920 px; nie stwierdzono poziomego przewijania całej strony.

Kontrola z 26 września użyła osobnego projektu Compose `reconflow-import-review` i wolumenu `reconflow-import-review_reconflow_pgdata`. API przez frontend zwróciło `test_instance=true`; jeden wybrany test Playwright zaimportował nowy syntetyczny rekord i ten sam plik ponownie. Wynik `1 passed (5,7 s)`. Po teście zatrzymano tylko kontenery testowe, pozostawiając wolumen. **Pełnego aktualnego zestawu Playwright przeciw Compose nie uruchomiono.**

## Granice dowodów

- Pytest zgłaszał jedno ostrzeżenie deprecacyjne zależności `starlette.testclient`/AnyIO; nie zmieniło ono wyniku testów.
- Historyczny benchmark 10 000 zamówień używał SQLite jako harnessu. [Zapisane liczby](benchmark-results.json) dotyczą tylko deterministycznych danych syntetycznych; nie potwierdzają wydajności PostgreSQL ani skuteczności na danych produkcyjnych.
- Projekt nie był audytowany pod kątem bezpieczeństwa wdrożenia publicznego. Nie ma logowania, ról, integracji finansowych ani produkcyjnej konfiguracji sekretów.
- W kopii publikacyjnej zachowano kod testów, zrzuty i zapis Compose, ale nie wszystkie lokalne logi historycznych przebiegów. Brak logu w repozytorium nie jest przedstawiany jako nowy wykonany test.
