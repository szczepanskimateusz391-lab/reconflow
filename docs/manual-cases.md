# Ręcznie opisane przypadki demonstracyjne

Wszystkie dane są syntetyczne. Oczekiwany moment analizy to `2025-02-15T12:00:00Z`, a tolerancja kwotowa `0,01`.

| Przypadek | Dane | Oczekiwany wynik |
|---|---|---|
| Poprawne dopasowanie | `shop-a/A-100`, płatność `P-100` | powiązanie po źródle i numerze; brak alertu finansowego |
| Niedopłata z numerem | `shop-a/A-101`, 100 z 150 PLN | powiązanie pozostaje; `underpayment_overdue` 50 PLN |
| Płatności częściowe | `shop-a/A-102`, 60 + 140 PLN | dwie transakcje przypisane do jednego zamówienia; saldo zgodne |
| Zwrot częściowy | `R-103`, oczekiwane 40, wykonane 20 PLN | `partial_refund` 20 PLN |
| Pending przed terminem | `P-104`, termin 2025-03-01 | brak alertu zaległości na moment analizy |
| Płatność failed | `P-105`, termin minął | failed nie jest sumowana; `missing_payment_overdue` |
| Dwóch równych kandydatów | `A-106`, `A-107`, `P-AMB` | `ambiguous_match`; brak automatycznego wyboru |
| Powtarzający się numer | `shop-a/A-100`, `shop-b/A-100` | dwa niezależne klucze i poprawne powiązania |
| Konflikt walut | `A-108` EUR, `P-108` PLN | powiązanie źródłowe zachowane, `currency_conflict`, bez odejmowania |
| Podejrzenie duplikatu | `P-109-A`, `P-109-B` | `suspected_duplicate_payment` mimo zgodnej sumy |
| Nadmierna refundacja | `R-110`, oczekiwane 20, wykonane 25 PLN | `refund_exceeds_expected` 5 PLN |
| Brak refundacji | `R-111`, termin minął | `refund_overdue` 15 PLN |
| Brak dokumentu | `A-112` | `missing_expected_document` po okresie karencji |
| Nadpłata | `A-113`, płatność 105 za 100 PLN | `overpayment` 5 PLN |
| Wypłata zbiorcza | `PAYOUT-1` | `separate_process_required`; brak dopasowania automatycznego |
| Ponowny import | ponowny import identycznego pliku | wszystkie rekordy `skipped`, brak duplikatów |
| Konflikt importu | `conflicting-order.csv` | odrzucony wiersz i `source_identifier_conflict`, bez nadpisania |
| Ponowne uzgadnianie | drugi przebieg bez zmian | te same alerty są aktualizowane, nie duplikowane |
| Decyzja ręczna | zatwierdzenie kandydata dla `P-AMB` | link z `origin=manual` pozostaje po kolejnym przebiegu |
| Podwójne przypisanie | próba przypisania tej samej płatności do innego zamówienia | HTTP 409, istniejące przypisanie bez zmian |

Pełna lista oczekiwanych alertów dla danych bazowych jest w `demo-data/expected-alerts.json`. Konflikt importu jest osobnym krokiem demonstracyjnym, dlatego nie znajduje się na bazowej liście.
