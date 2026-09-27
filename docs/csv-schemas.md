# Kontrakty CSV

Każdy plik jest kodowany jako UTF-8, używa przecinka i ma nagłówki dokładnie w podanej kolejności. Daty mają format ISO `YYYY-MM-DD`. Kwoty używają kropki dziesiętnej i maksymalnie dwóch miejsc po przecinku. Waluty to trzy wielkie litery. `source_system` jest częścią każdego klucza biznesowego; dzięki temu identyczne numery w dwóch sklepach nie są łączone.

## orders.csv

`source_system,order_id,sales_channel,order_date,due_date,status,gross_amount,currency,customer_name,customer_email`

Status: `new`, `confirmed`, `completed`, `cancelled`. Kwota brutto jest nieujemna. Dane klienta są opcjonalne i służą wyłącznie do budowy kandydatów.

## payments.csv

`source_system,transaction_id,kind,status,transaction_date,amount,currency,title,order_ref,order_source_system,return_ref,original_payment_ref,processing_scope`

`kind` to `payment` albo `refund` i określa kierunek przepływu. Kwota w pliku jest zawsze dodatnia. Do sum finansowych wchodzą tylko rekordy `completed`; pozostałe statusy to `pending` i `failed`. `processing_scope` ma wartość `direct`, `aggregate` lub `marketplace_payout`. Dwie ostatnie wartości są kierowane do osobnego procesu, nie do automatycznego dopasowania MVP. Jeśli `order_source_system` jest puste, przyjmowany jest `source_system` transakcji.

## documents.csv

`source_system,document_id,number,document_type,status,document_date,amount,currency,order_ref,order_source_system,original_document_ref`

Typ: `invoice`, `receipt`, `correction`; status: `draft`, `issued`, `cancelled`. Faktury i paragony mają kwoty nieujemne. Korekta ma wartość podpisaną: ujemna zmniejsza, dodatnia zwiększa łączną wartość dokumentów, i zawsze wskazuje `original_document_ref`. Do porównania wchodzą tylko dokumenty `issued`.

## returns.csv

`source_system,return_id,order_ref,order_source_system,return_date,status,expected_refund_amount,currency,refund_due_date,correction_ref`

Status: `requested`, `approved`, `received`, `cancelled`. Rekord opisuje oczekiwanie; wykonanie potwierdza wyłącznie `payments.csv` z `kind=refund` i `status=completed`. `correction_ref` jest opcjonalne.

## Semantyka importu

Identyczny rekord istniejący pod tym samym `(source_system, external_id)` jest pomijany. Zmieniona treść pod tym samym kluczem jest odrzucana jako konflikt i nie nadpisuje danych. Każdy błąd przechowuje nazwę pliku, numer wiersza, surowe wartości i przyczynę.
