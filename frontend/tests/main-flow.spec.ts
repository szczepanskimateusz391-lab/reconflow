import { expect, test } from '@playwright/test'
import path from 'node:path'

test('connection indicator distinguishes failures and retry restores the view', async ({ page }) => {
  let healthMode: 'database' | 'api' | 'available' = 'database'
  let failSummary = false
  await page.route('**/api/health', async (route) => {
    if (healthMode === 'database') {
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'degraded', api: 'available', database: 'unavailable', test_instance: true }),
      })
    } else if (healthMode === 'api') {
      await route.abort('connectionrefused')
    } else {
      await route.continue()
    }
  })
  await page.route('**/api/summary', async (route) => {
    if (failSummary) await route.abort('connectionrefused')
    else await route.continue()
  })

  await page.goto('/')
  await expect(page.getByTestId('api-state')).toHaveText(/Baza danych niedostępna/)
  await expect(page.getByTestId('fatal-error')).toContainText('nie może połączyć się z PostgreSQL')

  healthMode = 'api'
  await page.getByTestId('retry-load').click()
  await expect(page.getByTestId('api-state')).toHaveText(/API niedostępne/)
  await expect(page.getByTestId('fatal-error')).toContainText('/api/health')

  healthMode = 'available'
  failSummary = true
  await page.getByTestId('retry-load').click()
  await expect(page.getByTestId('api-state')).toHaveText(/API i baza działają · błąd widoku/)
  await expect(page.getByTestId('fatal-error')).toContainText('/api/summary')

  failSummary = false
  await page.getByTestId('retry-load').click()
  await expect(page.getByTestId('api-state')).toHaveText(/API i baza połączone/)
  await expect(page.getByText('Najpierw zaimportuj cztery pliki')).toBeVisible()
})

test.describe('analysis date in Europe/Warsaw', () => {
  test.use({ timezoneId: 'Europe/Warsaw' })

  test('last result and next-run input show the same moment until the user edits it', async ({ page }) => {
    let analysisAt = '2025-02-15T12:00:00Z'
    await page.route('**/api/health', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', api: 'available', database: 'available', test_instance: true }) }))
    await page.route('**/api/summary', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ run: { id: 1, analysis_at: analysisAt, started_at: '2025-02-15T12:00:00Z', completed_at: '2025-02-15T12:00:01Z', status: 'completed', tolerance: '0.01', document_grace_days: 3, metrics: {} }, by_currency: [], alerts_by_type: {}, active_alerts: 0, detected_cases: 0, priority_cases: [], data_context: { kind: 'demo', label: 'Dane demo', timeliness_note: 'Ocena na wybrany dzień.' } }) }))
    await page.route('**/api/imports', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }))
    await page.route('**/api/cases', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }))
    await page.route('**/api/reconciliations', async (route) => {
      const body = route.request().postDataJSON() as { analysis_at: string }
      expect(body.analysis_at).toBe('2025-02-15T11:00:00.000Z')
      analysisAt = body.analysis_at
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 2, analysis_at: analysisAt }) })
    })

    await page.goto('/')
    await expect(page.getByTestId('analysis-at')).toHaveValue('2025-02-15T13:00')
    await expect(page.getByTestId('analysis-context')).toContainText('15 lut 2025, 13:00')
    await expect(page.getByTestId('analysis-date-note')).toHaveCount(0)

    await page.getByTestId('analysis-at').fill('2025-02-15T12:00')
    await expect(page.getByTestId('analysis-date-note')).toContainText('Ostatni wynik: 15 lut 2025, 13:00')
    await expect(page.getByTestId('analysis-context')).toContainText('15 lut 2025, 13:00')

    await page.getByTestId('run-reconciliation').click()
    await expect(page.getByTestId('analysis-date-note')).toHaveCount(0)
    await expect(page.getByTestId('analysis-at')).toHaveValue('2025-02-15T12:00')
    await expect(page.getByTestId('analysis-context')).toContainText('15 lut 2025, 12:00')
  })
})

test('isolated import, reconciliation, resolution and rerun preserve money and decision', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText('Uzgodnij dane sprzedażowe')).toBeVisible()

  const healthResponse = await page.request.get('/api/health')
  expect(healthResponse.ok()).toBeTruthy()
  const health = await healthResponse.json() as { test_instance?: boolean }
  expect(health.test_instance, 'E2E refuses to modify a non-test ReconFlow instance').toBe(true)

  await page.getByTestId('nav-imports').click()
  for (const dataset of ['orders', 'payments', 'documents', 'returns']) {
    await page.getByTestId(`file-${dataset}`).setInputFiles(
      path.resolve(process.cwd(), 'tests', 'fixtures', `${dataset}.csv`),
    )
    await page.getByTestId(`upload-${dataset}`).click()
    await expect(page.getByTestId('import-result')).toContainText('Import zapisany')
  }

  await page.getByRole('button', { name: 'Przegląd' }).first().click()
  await page.getByTestId('run-reconciliation').click()
  await expect(page.getByText('Uzgadnianie zakończone')).toBeVisible()
  const resultDate = await page.getByTestId('analysis-context').locator('strong').last().innerText()
  await expect(page.getByTestId('active-case-count')).toContainText('1 aktywnych')
  await expect(page.getByTestId('amount-differences-PLN')).toHaveText(/50,00/)

  await page.getByTestId('nav-alerts').click()
  const underpaymentRow = page.getByRole('row').filter({ hasText: 'Niedopłata po terminie' })
  await expect(underpaymentRow).toBeVisible()
  await underpaymentRow.click()

  const drawer = page.getByTestId('alert-drawer')
  await expect(drawer.locator('.case-date-strip')).toContainText(resultDate)
  const calculation = drawer.getByTestId('case-calculation')
  await expect(calculation).toContainText('Wartość zamówienia')
  await expect(calculation).toContainText('150,00')
  await expect(calculation).toContainText('100,00')
  await expect(calculation).toContainText('50,00')
  await expect(calculation).toContainText('PLN')
  await expect(calculation).toContainText('10 sty 2025')
  await expect(calculation).toContainText('15 lut 2025')
  await expect(calculation).toContainText('P-101')
  await expect(calculation).toContainText('jednoznacznym order_ref')
  await expect(drawer.getByText('e2e-shop/D-101')).toBeVisible()

  const decisionComment = 'Audit restart persistence decision without confirmed recovery.'
  await drawer.getByLabel('Status').selectOption('resolved')
  await drawer.getByPlaceholder('Co sprawdzono i dlaczego podjęto tę decyzję?').fill(decisionComment)
  await drawer.getByRole('button', { name: 'Zapisz decyzję' }).click()
  await expect(drawer.getByText('Decyzja została zapisana. Kolejka, szczegóły i podsumowanie są aktualne.')).toBeVisible()
  await expect(drawer.getByText('Rozwiązany').first()).toBeVisible()
  await expect(drawer.getByText(decisionComment)).toBeVisible()
  await drawer.getByLabel('Zamknij szczegóły').click()
  await expect(page.getByTestId('filter-count')).toContainText('0 wyników')

  await page.getByRole('button', { name: 'Przegląd' }).first().click()
  await expect(page.getByTestId('active-case-count')).toContainText('0 aktywnych')
  await expect(page.getByTestId('amount-differences-PLN')).toHaveText(/50,00/)

  await page.screenshot({
    path: path.resolve(process.cwd(), '..', 'outputs', 'final-audit-overview.png'),
    fullPage: true,
  })
  await expect(page.getByText('Potwierdzony efekt działań').first()).toBeVisible()
  await page.getByTestId('run-reconciliation').click()
  await expect(page.getByText('Uzgadnianie zakończone')).toBeVisible()
  await expect(page.getByTestId('active-case-count')).toContainText('0 aktywnych')
  await expect(page.getByTestId('amount-differences-PLN')).toHaveText(/50,00/)

  const summary = await (await page.request.get('/api/summary')).json() as {
    active_alerts: number
    detected_cases: number
    by_currency: Array<{ currency: string; amount_differences: { total: string }; confirmed_user_effect: string }>
  }
  expect(summary.active_alerts).toBe(0)
  expect(summary.detected_cases).toBe(1)
  const pln = summary.by_currency.find((item) => item.currency === 'PLN')!
  expect(pln.amount_differences.total).toBe('50.00')
  expect(pln.confirmed_user_effect).toBe('0.00')

  const cases = await (await page.request.get('/api/cases')).json() as Array<{ id: string; handling_status: string }>
  expect(cases).toHaveLength(1)
  expect(cases[0].handling_status).toBe('resolved')
  const detail = await (await page.request.get(`/api/cases/${cases[0].id}`)).json() as {
    history: Array<{ action: string; comment: string | null }>
  }
  expect(detail.history.filter((item) => item.action === 'status_changed' && item.comment === decisionComment)).toHaveLength(1)

  await page.getByTestId('nav-alerts').click()
  await page.getByLabel('Status kolejki').selectOption('resolved')
  await expect(page.getByTestId('filter-count')).toContainText('1 wynik')
  const exportLink = page.getByRole('link', { name: 'Eksportuj widoczne przypadki CSV' })
  const exportHref = await exportLink.getAttribute('href')
  expect(exportHref).toContain('status=resolved')
  const exported = await (await page.request.get(exportHref!)).text()
  expect(exported).toContain('operational_case')
  expect(exported).toContain('underpayment_overdue')
  await page.screenshot({
    path: path.resolve(process.cwd(), '..', 'outputs', 'final-audit-queue.png'),
    fullPage: true,
  })
  await page.getByRole('row').filter({ hasText: 'Niedopłata po terminie' }).click()
  await expect(page.getByTestId('case-calculation')).toContainText('150,00')
  await page.screenshot({
    path: path.resolve(process.cwd(), '..', 'outputs', 'final-audit-a101-detail.png'),
    fullPage: true,
  })
})

test('failed and repeated decision writes are reported without duplicate history', async ({ page }) => {
  await page.goto('/')
  const health = await (await page.request.get('/api/health')).json() as { test_instance?: boolean }
  expect(health.test_instance).toBe(true)
  await page.getByTestId('nav-alerts').click()
  await page.getByLabel('Status kolejki').selectOption('resolved')
  await page.getByRole('row').filter({ hasText: 'Niedopłata po terminie' }).click()

  const drawer = page.getByTestId('alert-drawer')
  const failedComment = 'Ten komentarz nie może zostać zapisany.'
  await page.route('**/api/cases/*/status', async (route) => {
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Kontrolowany błąd zapisu' }),
    })
  })
  await drawer.getByPlaceholder('Co sprawdzono i dlaczego podjęto tę decyzję?').fill(failedComment)
  await drawer.getByRole('button', { name: 'Zapisz decyzję' }).click()
  await expect(drawer.getByText(/HTTP 500.*Kontrolowany błąd zapisu/)).toBeVisible()
  await expect(drawer.getByText('Decyzja została zapisana. Kolejka, szczegóły i podsumowanie są aktualne.')).not.toBeVisible()

  const cases = await (await page.request.get('/api/cases?status=resolved')).json() as Array<{ id: string }>
  let detail = await (await page.request.get(`/api/cases/${cases[0].id}`)).json() as {
    history: Array<{ comment: string | null }>
  }
  expect(detail.history.some((item) => item.comment === failedComment)).toBe(false)

  await page.unroute('**/api/cases/*/status')
  const repeatedComment = 'Podwójne kliknięcie ma dać jeden wpis historii.'
  await drawer.getByPlaceholder('Co sprawdzono i dlaczego podjęto tę decyzję?').fill(repeatedComment)
  await drawer.getByRole('button', { name: 'Zapisz decyzję' }).dblclick()
  await expect(drawer.getByText('Decyzja została zapisana. Kolejka, szczegóły i podsumowanie są aktualne.')).toBeVisible()
  detail = await (await page.request.get(`/api/cases/${cases[0].id}`)).json() as {
    history: Array<{ comment: string | null }>
  }
  expect(detail.history.filter((item) => item.comment === repeatedComment)).toHaveLength(1)
})

test('new payment changes the current result while history survives time-travel reruns', async ({ page }) => {
  await page.goto('/')
  const health = await (await page.request.get('/api/health')).json() as { test_instance?: boolean }
  expect(health.test_instance).toBe(true)

  const extraPayment = Buffer.from(
    'source_system,transaction_id,kind,status,transaction_date,amount,currency,title,order_ref,order_source_system,return_ref,original_payment_ref,processing_scope\n' +
    'e2e-shop,P-101-EXTRA,payment,completed,2025-02-20,50.00,PLN,Dopłata do A-101,A-101,e2e-shop,,,direct\n',
    'utf8',
  )
  const imported = await page.request.post('/api/imports/payments', {
    multipart: { file: { name: 'payments-after-analysis.csv', mimeType: 'text/csv', buffer: extraPayment } },
  })
  expect(imported.ok()).toBeTruthy()
  expect((await imported.json()).imported_count).toBe(1)

  const after = await page.request.post('/api/reconciliations', {
    data: { analysis_at: '2025-02-25T12:00:00Z' },
  })
  expect(after.ok()).toBeTruthy()
  let summary = await (await page.request.get('/api/summary')).json() as {
    active_alerts: number
    by_currency: Array<{ currency: string; amount_differences: { total: string } }>
  }
  expect(summary.active_alerts).toBe(1)
  expect(summary.by_currency.find((item) => item.currency === 'PLN')?.amount_differences.total).toBe('0.00')

  await page.reload()
  await page.getByTestId('nav-alerts').click()
  await expect(page.getByRole('row').filter({ hasText: 'Wynik zamkniętej sprawy zmienił się' })).toBeVisible()
  await page.getByRole('row').filter({ hasText: 'Wynik zamkniętej sprawy zmienił się' }).click()
  const drawer = page.getByTestId('alert-drawer')
  await expect(drawer.getByTestId('case-calculation')).toContainText('150.00 − 150.00 = 0.00 PLN')
  await expect(drawer.getByText('Historyczne').first()).toBeVisible()
  await expect(drawer.getByText('Audit restart persistence decision without confirmed recovery.')).toBeVisible()
  await drawer.getByLabel('Zamknij szczegóły').click()

  const beforeAgain = await page.request.post('/api/reconciliations', {
    data: { analysis_at: '2025-02-15T12:00:00Z' },
  })
  expect(beforeAgain.ok()).toBeTruthy()
  summary = await (await page.request.get('/api/summary')).json()
  expect(summary.active_alerts).toBe(1)
  expect(summary.by_currency.find((item) => item.currency === 'PLN')?.amount_differences.total).toBe('50.00')

  await page.reload()
  await page.getByTestId('nav-alerts').click()
  const reopened = page.getByRole('row').filter({ hasText: 'Niedopłata po terminie' })
  await expect(reopened).toContainText('W toku')
  await reopened.click()
  await expect(page.getByTestId('case-calculation')).toContainText('150.00 − 100.00 = 50.00 PLN')
  await expect(page.getByTestId('case-calculation')).toContainText('P-101')
  await expect(page.getByTestId('case-calculation')).not.toContainText('P-101-EXTRA')
  await expect(page.getByText('Ustalenie ponownie stało się bieżące')).toBeVisible()
  await expect(page.getByText('Audit restart persistence decision without confirmed recovery.')).toBeVisible()
})

test('identical reimport explains skipped records only when the count is positive', async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 900 })
  await page.goto('/')
  const health = await (await page.request.get('/api/health')).json() as { test_instance?: boolean }
  expect(health.test_instance, 'Import review must use an isolated test instance').toBe(true)

  const orderId = `SKIP-${Date.now()}`
  const csv = Buffer.from(
    'source_system,order_id,sales_channel,order_date,due_date,status,gross_amount,currency,customer_name,customer_email\n' +
    `import-review,${orderId},web,2025-01-02,2025-01-10,completed,150.00,PLN,Klient Testowy,test@example.test\n`,
    'utf8',
  )
  const importFile = { name: 'orders-repeat-review.csv', mimeType: 'text/csv', buffer: csv }
  const message = 'Pominięto identyczne rekordy, które były już w bazie.'

  await page.getByTestId('nav-imports').click()
  await page.getByTestId('file-orders').setInputFiles(importFile)
  const firstResponse = page.waitForResponse((response) => response.url().endsWith('/api/imports/orders') && response.request().method() === 'POST')
  await page.getByTestId('upload-orders').click()
  const first = await (await firstResponse).json() as { imported_count: number; skipped_count: number; rejected_count: number }
  expect(first).toMatchObject({ imported_count: 1, skipped_count: 0, rejected_count: 0 })
  const result = page.getByTestId('import-result')
  await expect(result.locator('dd').nth(1)).toHaveText('0')
  await expect(result.getByText(message, { exact: true })).toHaveCount(0)
  await page.screenshot({
    path: path.resolve(process.cwd(), '..', 'docs', 'evidence', 'import-skipped-first.png'),
    fullPage: true,
  })

  const secondResponse = page.waitForResponse((response) => response.url().endsWith('/api/imports/orders') && response.request().method() === 'POST')
  await page.getByTestId('upload-orders').click()
  const second = await (await secondResponse).json() as { imported_count: number; skipped_count: number; rejected_count: number }
  expect(second).toMatchObject({ imported_count: 0, skipped_count: 1, rejected_count: 0 })
  await expect(result.locator('dd').nth(1)).toHaveText('1')
  await expect(result.getByText(message, { exact: true })).toBeVisible()
  await page.screenshot({
    path: path.resolve(process.cwd(), '..', 'docs', 'evidence', 'import-skipped-repeat.png'),
    fullPage: true,
  })
})
