import { chromium } from '@playwright/test'
import { mkdir } from 'node:fs/promises'
import path from 'node:path'

const baseURL = process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:5173'
const outputDirectory = process.env.SCREENSHOT_OUTPUT_DIR
  ? path.resolve(process.env.SCREENSHOT_OUTPUT_DIR)
  : path.resolve(process.cwd(), '..', 'outputs')

await mkdir(outputDirectory, { recursive: true })

const browser = await chromium.launch({ headless: true })
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, timezoneId: 'Europe/Warsaw' })

try {
  await page.goto(baseURL)
  await page.getByText('API i baza połączone').waitFor()
  await page.getByTestId('active-case-count').waitFor()
  await page.getByRole('heading', { name: 'Podsumowanie według waluty' }).waitFor()
  await page.screenshot({ path: path.join(outputDirectory, 'reconflow-podsumowanie.png'), fullPage: true })

  await page.getByTestId('nav-alerts').click()
  await page.setViewportSize({ width: 1366, height: 1000 })
  await page.getByTestId('filter-count').waitFor()
  await page.screenshot({ path: path.join(outputDirectory, 'reconflow-kolejka.png'), fullPage: false })

  await page.getByLabel('Status kolejki').selectOption('resolved')
  await page.setViewportSize({ width: 1440, height: 2400 })
  await page.locator('tbody tr').filter({ hasText: 'Niedopłata po terminie' }).click()
  await page.getByTestId('case-calculation').waitFor()
  await page.locator('.timeline li').nth(1).waitFor()
  await page.getByTestId('alert-drawer').screenshot({ path: path.join(outputDirectory, 'reconflow-niedoplata-50.png') })
} finally {
  await browser.close()
}
