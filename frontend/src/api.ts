export type CurrencySummary = {
  currency: string
  controlled_order_value: string
  amount_differences: AmountSummary
  requires_explanation: AmountSummary
  separate_process: AmountSummary
  detected_discrepancy_exposure: string
  confirmed_user_effect: string
  confirmed_user_effect_items: Array<{
    case_id: string
    title: string
    amount: string
    currency: string
    note: string
  }>
}

export type AmountSummaryItem = {
  case_id: string
  title: string
  rule: string
  amount: string
  currency: string
  handling_status: Alert['handling_status']
  calculation: string
  source_records: SourceRef[]
  deduplicated_alert_ids: number[]
  basis: string
}

export type AmountSummary = {
  total: string
  items: AmountSummaryItem[]
  rule: string
}

export type Summary = {
  run: null | {
    id: number
    analysis_at: string
    started_at: string
    completed_at: string | null
    status: string
    tolerance: string
    document_grace_days: number
    metrics: Record<string, number>
  }
  by_currency: CurrencySummary[]
  alerts_by_type: Record<string, number>
  active_alerts: number
  detected_cases: number
  priority_cases: OperationalCase[]
  data_context: {
    kind: 'demo' | 'local' | 'test'
    label: string
    timeliness_note: string
  }
}

export type ImportBatch = {
  id: number
  dataset: string
  file_name: string
  status: string
  imported_count: number
  skipped_count: number
  conflict_count: number
  rejected_count: number
  created_at: string
  completed_at: string | null
}

export type SourceRef = {
  record_type: string
  id: number
  source_system: string
  external_id: string
  source_file: string
  source_row: number
}

export type Alert = {
  id: number
  alert_type: string
  title: string
  description: string
  evidence: Record<string, unknown>
  source_records: SourceRef[]
  entity_type: string
  entity_id: number
  order_id: number | null
  currency: string | null
  discrepancy_amount: string | null
  handling_status: 'new' | 'in_progress' | 'resolved' | 'ignored'
  confirmed_effect_amount: string | null
  active: boolean
  first_seen_run_id: number
  last_seen_run_id: number
  created_at: string
  updated_at: string
}

export type CaseAmount = { currency: string; amount: string }

export type OperationalCase = {
  id: string
  title: string
  description: string
  handling_status: Alert['handling_status']
  active: boolean
  finding_count: number
  alert_types: string[]
  findings: Alert[]
  source_records: SourceRef[]
  amounts: CaseAmount[]
  deadline: string | null
  days_overdue: number | null
  updated_at: string
  order_id: number | null
  priority: 'high' | 'medium' | 'low'
  priority_reason: string
}

export type Candidate = {
  id: number
  order_id: number
  rule_score: number
  rule_breakdown: Record<string, { points: number; reason: string }>
  status: string
  order: Record<string, unknown>
}

export type CaseDetail = OperationalCase & {
  historical_finding_count: number
  records: Record<string, unknown>[]
  history: Array<{
    id: number
    alert_id: number | null
    alert_title: string
    action: string
    previous_status: string | null
    new_status: string | null
    comment: string | null
    actor: string
    details: Record<string, unknown> | null
    created_at: string
  }>
  candidates: Candidate[]
  analysis_at: string
  run_started_at: string
  run_completed_at: string | null
  calculation: null | {
    order_value: string
    completed_payment_total: string
    missing_amount: string
    currency: string
    equation: string
    due_date: string
    analysis_at: string
    tolerance: string
    completed_payments: Array<{
      transaction_id: string
      status: string
      amount: string
      currency: string
      transaction_date: string
      source_file: string
      source_row: number
      matching_explanation: string
    }>
    other_currency_payments: Array<{
      transaction_id: string
      amount: string
      currency: string
      matching_explanation: string
    }>
  }
}

const API = import.meta.env.VITE_API_URL || '/api'

export type Health = {
  status: 'ok' | 'degraded'
  api: 'available'
  database: 'available' | 'unavailable'
  test_instance: boolean
}

export class ApiRequestError extends Error {
  constructor(
    message: string,
    public readonly kind: 'network' | 'http',
    public readonly url: string,
    public readonly status: number | null = null,
  ) {
    super(message)
    this.name = 'ApiRequestError'
  }
}

function requestUrl(path: string) {
  const rawUrl = `${API}${path}`
  return { rawUrl, displayUrl: new URL(rawUrl, window.location.origin).toString() }
}

async function fetchResponse(path: string, options?: RequestInit): Promise<Response> {
  const { rawUrl, displayUrl } = requestUrl(path)
  try {
    return await fetch(rawUrl, options)
  } catch {
    throw new ApiRequestError(
      `Brak odpowiedzi z ${displayUrl} (błąd połączenia).`,
      'network',
      displayUrl,
    )
  }
}

async function responseDetail(response: Response): Promise<string> {
  try {
    const payload = await response.json() as { detail?: string }
    return payload.detail || `HTTP ${response.status}`
  } catch {
    return `HTTP ${response.status}`
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetchResponse(path, options)
  if (!response.ok) {
    const { displayUrl } = requestUrl(path)
    const detail = await responseDetail(response)
    throw new ApiRequestError(
      `Żądanie ${displayUrl} zakończyło się błędem HTTP ${response.status}: ${detail}.`,
      'http',
      displayUrl,
      response.status,
    )
  }
  return response.json() as Promise<T>
}

export const api = {
  health: async () => {
    const path = '/health'
    const response = await fetchResponse(path)
    if (response.status === 503) return response.json() as Promise<Health>
    if (!response.ok) {
      const { displayUrl } = requestUrl(path)
      const detail = await responseDetail(response)
      throw new ApiRequestError(
        `Kontrola API ${displayUrl} zakończyła się błędem HTTP ${response.status}: ${detail}.`,
        'http',
        displayUrl,
        response.status,
      )
    }
    return response.json() as Promise<Health>
  },
  summary: () => request<Summary>('/summary'),
  imports: () => request<ImportBatch[]>('/imports'),
  alerts: (query = '') => request<Alert[]>(`/alerts${query}`),
  cases: (query = '') => request<OperationalCase[]>(`/cases${query}`),
  case: (id: string) => request<CaseDetail>(`/cases/${id}`),
  reconcile: (analysisAt?: string) =>
    request<{ id: number; status: string }>('/reconciliations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ analysis_at: analysisAt || null }),
    }),
  upload: (dataset: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<ImportBatch>(`/imports/${dataset}`, { method: 'POST', body: form })
  },
  updateCase: (id: string, status: string, comment: string, confirmedEffect?: string) =>
    request<CaseDetail>(`/cases/${id}/status`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        status,
        comment,
        confirmed_effect_amount: confirmedEffect || null,
      }),
    }),
  decideCandidate: (id: number, decision: 'approve' | 'reject', comment: string) =>
    request<{ status: string }>(`/candidates/${id}/decision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, comment }),
    }),
  manualLink: (paymentId: number, orderId: number, comment: string) =>
    request<{ status: string }>(`/payments/${paymentId}/link`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ order_id: orderId, comment }),
    }),
}
