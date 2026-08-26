export interface User {
  id: string
  email: string
  is_active: boolean
  is_superuser: boolean
  is_verified: boolean
  is_2fa_enabled: boolean
  preferences: UserPreferences
}

export interface AdminUser {
  id: string
  email: string
  is_active: boolean
  is_superuser: boolean
  is_verified: boolean
  preferences: UserPreferences | null
}

export interface AdminUserList {
  items: AdminUser[]
  total: number
}

export interface Passkey {
  id: string
  name: string
  transports: string[] | null
  aaguid: string | null
  device_type: string | null
  backed_up: boolean | null
  created_at: string
  last_used_at: string | null
}

export interface PasskeyOptionsResponse {
  challenge_id: string
  options: Record<string, unknown>
}

export interface AppSetting {
  key: string
  value: string
}

export type WorkspaceRole = 'owner' | 'editor' | 'viewer' | 'manager'

export type WorkspaceKind = 'personal' | 'business'

export interface Workspace {
  id: string
  name: string
  // Widened on purpose: a workspace stored before the current kind list
  // still has to render. Writes are narrowed to WorkspaceKind.
  kind: string
  is_archived: boolean
  default_currency: string
  locale: string | null
  /** Where the workspace files. Selects the fiscal document pack; never the
   *  interface language. */
  tax_jurisdiction: string | null
  icon: string | null
  color: string | null
  created_at: string
  created_by_user_id: string | null
  managed_by_user_id: string | null
  role: WorkspaceRole | null
  /** Modules this workspace shows. Resolved server-side; see lib/modules.ts. */
  enabled_modules: string[]
}

export interface WorkspaceMember {
  id: string
  user_id: string
  email: string
  display_name: string | null
  role: WorkspaceRole
  joined_at: string
}

export interface UserPreferences {
  language?: string
  date_format?: string
  timezone?: string
  currency_display?: string
  display_name?: string
  onboarding_completed?: boolean
}

export interface Category {
  id: string
  user_id: string
  group_id: string | null
  name: string
  icon: string
  color: string
  is_system: boolean
  treat_as_transfer: boolean
  is_ignored: boolean
}

export interface CategoryGroup {
  id: string
  user_id: string
  name: string
  icon: string
  color: string
  position: number
  is_system: boolean
  categories: Category[]
}

export interface BankConnection {
  id: string
  user_id: string
  provider: string
  institution_name: string
  display_name: string | null
  logo_url: string | null
  external_id: string
  status: string
  settings: ConnectionSettings | null
  pending_account_count: number
  last_sync_at: string | null
  last_sync_error_account_id: string | null
  created_at: string
}

export interface ConnectionSettings {
  payee_source?: 'auto' | 'merchant' | 'payment_data' | 'description' | 'none'
  import_pending?: boolean
  sync_assets?: boolean
  // Absent means the connection syncs every provider account (legacy); an
  // empty list means it syncs none. The connection also carries the seen and
  // reviewed id sets sync records, but only the backend reads those — they
  // reach the frontend already reduced to `pending_account_count`.
  account_allowlist?: string[]
  // Write-only, and only alongside account_allowlist: the ids the picker had on
  // screen when the user saved. An account that turned up at the provider since
  // the last sync is in no set the backend holds, so without this one the user
  // unchecked would come back as pending. Never read back.
  reviewed_account_ids?: string[]
}

export interface ProviderAccount {
  external_id: string
  name: string
  balance: string
  currency: string
  // null where the provider does not say — not the same as "no holdings".
  has_holdings: boolean | null
  status: 'included' | 'excluded' | 'pending'
}

export interface Account {
  id: string
  user_id: string
  connection_id: string | null
  external_id: string | null
  name: string
  display_name: string | null
  // Last 4 chars of the bank's identifier for the account, when the provider
  // exposes one. Tells apart accounts a bank reports under an identical name.
  masked_number: string | null
  // Denormalized bank identity from the linked connection (null for manual
  // accounts). Used to render the institution logo next to the account.
  institution_name: string | null
  institution_logo_url: string | null
  type: string
  balance: number
  current_balance: number
  previous_balance: number | null
  balance_primary: number | null
  currency: string
  credit_limit: number | null
  available_credit: number | null
  statement_close_day: number | null
  payment_due_day: number | null
  next_close_date: string | null
  next_due_date: string | null
  minimum_payment: number | null
  card_brand: string | null
  card_level: string | null
  is_closed: boolean
  closed_at: string | null
}

export interface CreditCardBill {
  id: string
  account_id: string
  external_id: string
  due_date: string // YYYY-MM-DD
  total_amount: number
  currency: string
  minimum_payment: number | null
}

export interface Collection {
  id: string
  user_id: string
  name: string
  icon: string
  color: string
  position: number
  account_ids: string[]
  account_count: number
  wallet_ids: string[]
  wallet_count: number
}

export interface AccountSummary {
  account_id: string
  current_balance: number
  opening_balance: number
  monthly_income: number
  monthly_expenses: number
  current_balance_primary: number | null
  opening_balance_primary: number | null
  monthly_income_primary: number | null
  monthly_expenses_primary: number | null
  projected_income?: number
  projected_expenses?: number
  projected_income_primary?: number | null
  projected_expenses_primary?: number | null
}

export interface Transaction {
  id: string
  user_id: string
  account_id: string | null
  category_id: string | null
  category: Category | null
  external_id: string | null
  description: string
  original_description: string | null
  amount: number
  currency: string
  date: string
  effective_date: string
  type: 'debit' | 'credit'
  source: string
  status: 'posted' | 'pending'
  payee: string | null
  payee_id: string | null
  payee_name: string | null
  notes: string | null
  transfer_pair_id: string | null
  amount_primary: number | null
  fx_rate_used: number | null
  fx_fallback: boolean
  attachment_count?: number
  installment_number: number | null
  total_installments: number | null
  installment_total_amount: number | null
  installment_purchase_date: string | null
  installment_series_id: string | null
  bill_id: string | null
  // Manual override for which credit-card bill cycle this tx belongs to
  // (issue #92). Empty / null = use auto bucketing (Pluggy bill_id when
  // available, cycle math otherwise). Setting it forces the tx into the
  // bill whose due_date matches.
  effective_bill_date: string | null
  // The recurring bill this transaction fulfills, if any (issue #116).
  recurring_transaction_id?: string | null
  splits: TransactionSplit[]
  // Shared-transaction view fields. Set per-request when the viewer
  // is a linked split member but not the owner. Render `viewer_share`
  // as the amount and treat the row as read-only — editing belongs
  // to the parent's owner.
  is_shared?: boolean
  viewer_share?: number | null
  group_id?: string | null
  // Display name of the parent's owner (the person who actually paid).
  // Derived per-request from the group's `is_self` member.
  parent_owner_name?: string | null
  // Flag to exclude this transaction from reports and dashboard aggregations
  is_ignored: boolean
  virtual?: boolean
}

// Scope for installment-series edits/deletes: "this" (default) only touches
// the target row, "future" touches it plus later installments, "all" touches
// the whole series. Ignored server-side for non-installment transactions.
export type TransactionApplyScope = 'this' | 'future' | 'all'

// Payload for POST /api/transactions/installments. `base` is
// the amount repeated as-is; the backend fans it out into `installments`
// equal parcels sharing the installment fingerprint and stores
// installment_total_amount = base.amount * installments.
export interface InstallmentSeriesInput {
  base: {
    account_id: string
    category_id?: string | null
    payee_id?: string | null
    description: string
    amount: number
    date: string
    type: 'debit' | 'credit'
    currency?: string
    notes?: string | null
    status?: 'posted' | 'pending'
    amount_primary?: number | null
    fx_rate_used?: number | null
    effective_bill_date?: string | null
    splits?: TransactionSplitsInput | null
  }
  installments: number
  first_installment_status?: 'posted' | 'pending'
  frequency?: 'monthly' | 'quarterly' | 'weekly' | 'yearly'
}

export type ShareType = 'equal' | 'exact' | 'percent'

export interface TransactionSplit {
  id: string
  transaction_id: string
  group_member_id: string
  share_amount: number
  share_type: string
  share_pct: number | null
  notes: string | null
  created_at: string
}

export interface TransactionSplitInput {
  group_member_id: string
  share_amount?: number | null
  share_pct?: number | null
  notes?: string | null
}

export interface TransactionSplitsInput {
  share_type: ShareType
  splits: TransactionSplitInput[]
}

// Payload the transaction dialog sends on save. `splits` is the normalized
// TransactionSplitsInput the split section produces, not the
// TransactionSplit[] rows the API returns, so the edit payload type reflects
// the form's actual shape.
export type TransactionEditPayload = Omit<Partial<Transaction>, 'splits'> & {
  splits?: TransactionSplitsInput | null
}

export type GroupKind = 'social' | 'cost_center' | 'project' | 'client' | 'other'

export interface Group {
  id: string
  user_id: string
  name: string
  kind: GroupKind
  default_currency: string
  icon: string
  color: string
  is_archived: boolean
  // Derived server-side per request. False = the current user is a
  // linked member, not the owner — UI should hide edit affordances.
  is_owner: boolean
  notes: string | null
  created_at: string
  members: GroupMember[]
}

export interface GroupMember {
  id: string
  group_id: string
  name: string
  linked_user_id: string | null
  email: string | null
  is_self: boolean
  created_at: string
}

export interface GroupSettlement {
  id: string
  group_id: string
  from_member_id: string
  to_member_id: string
  amount: number
  currency: string
  date: string
  transaction_id: string | null
  notes: string | null
  created_at: string
}

export interface GroupBalanceLine {
  member_id: string
  currency: string
  // Positive = member owes the owner. Negative = owner owes member.
  amount: number
  // FX-converted to the group's default currency for cross-currency rollups.
  amount_in_default_currency: number
}

export interface GroupBalances {
  group_id: string
  self_member_id: string | null
  default_currency: string
  lines: GroupBalanceLine[]
}

/** A fiscal document belonging to a payee. `kind` mirrors the backend's
 *  closed TaxIdKind; the value arrives normalised. */
export interface PayeeTaxId {
  kind: string
  value: string
}

export interface Payee {
  id: string
  user_id: string
  name: string
  /** Legal nature, or null when unknown — the normal state for a row sync created. */
  type: 'person' | 'company' | null
  /** Where the row came from. Server-set at creation and never editable. */
  source: 'manual' | 'sync' | 'import'
  is_favorite: boolean
  notes: string | null
  email: string | null
  phone: string | null
  address: string | null
  website: string | null
  tax_ids: PayeeTaxId[]
  created_at: string
  transaction_count: number
}

/** One document kind as the active workspace's jurisdiction describes it.
 *  `offered` marks the ones its pack asks for; the rest stay selectable,
 *  because a counterparty's country is not the workspace's. */
export interface TaxIdKindOption {
  kind: string
  label_key: string
  mask: string | null
  offered: boolean
}

export interface PayeeSummary {
  payee: Payee
  total_spent: number
  total_received: number
  transaction_count: number
  most_common_category: Category | null
  last_transaction_date: string | null
}

export interface RuleCondition {
  field: string
  op: string
  value: string | number
}

/** A nested group of conditions joined by its own operator.
 *
 * Groups let a rule mix AND and OR — `type is debit AND (contains UBER OR
 * contains 99POP)`. They hold leaf conditions only, capping rule depth at the
 * two levels the engine evaluates and the editor exposes.
 */
export interface RuleConditionGroup {
  op: 'and' | 'or'
  conditions: RuleCondition[]
}

/** An entry of a rule's condition list: a leaf condition or one group. */
export type RuleConditionNode = RuleCondition | RuleConditionGroup

export interface RuleAction {
  op: string
  value: string
}

export interface Rule {
  id: string
  user_id: string
  name: string
  conditions_op: 'and' | 'or'
  conditions: RuleConditionNode[]
  actions: RuleAction[]
  priority: number
  is_active: boolean
  apply_to_existing?: boolean
  overwrite_existing_categories?: boolean
}

export interface RuleExportItem {
  name: string
  conditions_op: 'and' | 'or'
  conditions: RuleConditionNode[]
  actions: RuleAction[]
  priority: number
  is_active: boolean
}

export interface RuleExportPayload {
  format: 'securo-categorization-rules'
  version: number
  rules: RuleExportItem[]
}

export interface RuleImportResponse {
  imported: number
  skipped: number
  overwritten: number
}

export interface ImportLog {
  id: string
  user_id: string
  /** Null for an order import, which lands on holdings rather than an account. */
  account_id: string | null
  account_name: string | null
  entity: 'transactions' | 'asset_orders'
  filename: string
  format: string
  transaction_count: number
  total_credit: number
  total_debit: number
  created_at: string
}

export interface ImportPreviewTransaction {
  description: string
  amount: number
  date: string
  type: 'debit' | 'credit'
  external_id?: string | null
  currency?: string | null
  fx_rate?: number | null
  payee_raw?: string | null
  category_name?: string | null
  suggested_category_id?: string | null
  suggested_category_name?: string | null
  excluded?: boolean
  category_id?: string | null
  force_uncategorized?: boolean
  notes?: string | null
}

export interface ImportReviewTransaction extends ImportPreviewTransaction {
  _id: string
  excluded: boolean
  selected_category_id?: string | null
}

export interface RecurringTransaction {
  id: string
  user_id: string
  account_id: string | null
  category_id: string | null
  description: string
  amount: number
  currency: string
  type: 'debit' | 'credit'
  frequency: 'monthly' | 'quarterly' | 'weekly' | 'yearly'
  weekend_adjustment: 'none' | 'previous_friday' | 'next_monday'
  day_of_month: number | null
  start_date: string
  end_date: string | null
  is_active: boolean
  auto_generate: boolean
  next_occurrence: string
  amount_primary: number | null
  fx_rate_used: number | null
}

export interface ProjectedTransaction {
  recurring_id: string
  account_id: string | null
  description: string
  amount: number
  amount_primary: number | null
  currency: string
  type: 'debit' | 'credit'
  date: string
  category_id: string | null
  category_name: string | null
  category_icon: string | null
  category_color: string | null
}

export interface TransactionCalendarItem {
  kind: 'actual' | 'projected'
  id: string | null
  recurring_id: string | null
  date: string
  description: string
  amount: number
  amount_primary: number | null
  currency: string
  type: 'debit' | 'credit'
  account_id: string | null
  account_name: string | null
  category_id: string | null
  category_name: string | null
  category_icon: string | null
  category_color: string | null
  status: string | null
  source: string | null
  transfer_pair_id: string | null
  is_transfer: boolean
  is_ignored: boolean
}

export interface TransactionCalendarDay {
  date: string
  in_month: boolean
  ending_balance: number
  // Combined totals kept for backwards compatibility.
  income: number
  expense: number
  transfer_net: number
  actual_income: number
  actual_expense: number
  actual_transfer_net: number
  projected_income: number
  projected_expense: number
  projected_transfer_net: number
  actual_count: number
  projected_count: number
  has_income: boolean
  has_expense: boolean
  has_transfer: boolean
  items: TransactionCalendarItem[]
}

export interface TransactionCalendarResponse {
  month: string
  currency: string
  account_ids: string[] | null
  days: TransactionCalendarDay[]
}

export interface DashboardSummary {
  total_balance: Record<string, number>
  total_balance_primary: number
  projected_balance: Record<string, number>
  projected_balance_primary: number
  balance_date: string
  monthly_income: number
  monthly_expenses: number
  monthly_income_primary: number
  monthly_expenses_primary: number
  projected_income?: number
  projected_expenses?: number
  projected_income_primary?: number
  projected_expenses_primary?: number
  accounts_count: number
  pending_categorization: number
  pending_categorization_amount: number
  assets_value: Record<string, number>
  assets_value_primary: number
  primary_currency: string
  // Net pending balance from group splits in primary currency.
  // Negative = net liability, positive = net receivable. Already
  // accounts for partial settlements.
  pending_shares_net: number
}

export interface SpendingByCategory {
  category_id: string | null
  category_name: string
  category_icon: string
  category_color: string
  total: number
  percentage: number
}

export interface MonthlyTrend {
  month: string
  income: number
  expenses: number
}

export interface DailyBalance {
  day: number
  balance: number | null
}

export interface BalanceHistory {
  current: DailyBalance[]
  previous: DailyBalance[]
}

export interface Budget {
  id: string
  user_id: string
  category_id: string
  amount: number
  month: string
  is_recurring: boolean
}

export interface BudgetVsActual {
  category_id: string
  category_name: string
  category_icon: string
  category_color: string
  group_id: string | null
  group_name: string | null
  budget_amount: number | null
  actual_amount: number
  projected_amount: number
  prev_month_amount: number
  projected_prev_month_amount: number
  percentage_used: number | null
  is_recurring: boolean
}

export interface Asset {
  id: string
  user_id: string
  name: string
  type: string
  currency: string
  units: number | null
  valuation_method: string
  purchase_date: string | null
  purchase_price: number | null
  sell_date: string | null
  sell_price: number | null
  growth_type: string | null
  growth_rate: number | null
  growth_frequency: string | null
  growth_start_date: string | null
  is_archived: boolean
  position: number
  current_value: number | null
  current_value_primary: number | null
  gain_loss: number | null
  gain_loss_primary: number | null
  value_count: number
  source: string
  connection_id: string | null
  isin: string | null
  maturity_date: string | null
  group_id: string | null
  ticker: string | null
  ticker_exchange: string | null
  last_price: number | null
  last_price_at: string | null
  value_updated_at: string | null
  logo_url: string | null
  // Ledger-derived (issue #235): weighted-average cost per unit (preço médio),
  // cost basis of held units, cumulative realized gain, and whether the holding
  // is driven by the transactions ledger.
  average_price: number | null
  total_invested: number | null
  realized_gain: number | null
  transaction_count: number
}

/** One order read from a broker CSV, before it reaches a holding. */
export interface AssetOrderImport {
  row: number
  ticker: string
  date: string
  kind: 'buy' | 'sell'
  quantity: number
  price: number
  fee: number
  currency: string | null
  name: string | null
  notes: string | null
  external_id: string | null
}

export interface AssetImportRowError {
  row: number
  reason: string
  ticker: string | null
  detail: string | null
}

export interface AssetImportSkip {
  row: number
  reason: string
  ticker: string | null
  detail: string | null
}

export interface AssetImportWarning {
  ticker: string
  reason: string
  wallet: string | null
  imported_units: string | null
  reported_units: string | null
}

export interface AssetImportPreview {
  orders: AssetOrderImport[]
  errors: AssetImportRowError[]
  skips: AssetImportSkip[]
  warnings: AssetImportWarning[]
  csv_columns: string[]
  parse_error: string | null
  holdings_created: number
  holdings_matched: number
  skipped: number
}

export interface AssetImportResult {
  imported: number
  skipped: number
  holdings_created: number
  holdings_matched: number
  errors: AssetImportRowError[]
  skips: AssetImportSkip[]
  warnings: AssetImportWarning[]
}

export interface AssetTransaction {
  id: string
  asset_id: string
  kind: 'buy' | 'sell'
  quantity: number
  price: number
  fee: number
  date: string
  source: string
  notes: string | null
  asset_name: string | null
  ticker: string | null
  currency: string | null
  logo_url: string | null
}

/** One acquisition, derived by replaying the ledger — never a stored record. */
export interface TaxLot {
  acquired: string
  quantity: number
  unit_price: number
  cost: number
  holding_days: number
  long_term: boolean
  /** Zero once the lot is long-term. */
  days_until_long_term: number
}

export interface TaxLotSale {
  date: string
  quantity: number
  gain: number
  long_quantity: number
  short_quantity: number
  long_gain: number
  short_gain: number
}

export interface TaxLots {
  asset_id: string
  ticker: string | null
  /** False when the wallet is not Taxable — the gain there has no tax character, so no lots are reported. */
  tax_character: boolean
  /** Provider-reported position with no trades behind it: holding period is unknown, not short. */
  snapshot: boolean
  as_of: string
  lots: TaxLot[]
  long_quantity: number
  short_quantity: number
  long_cost: number
  short_cost: number
  sales: TaxLotSale[]
  realised_long: number
  realised_short: number
}

/** A wallet the warning names: it bought inside the window, or it holds the instrument now. */
export interface WashSaleWallet {
  wallet: string | null
  wallet_id: string
  tax_treatment: string | null
  /** The wallet is tax-advantaged, so a loss disallowed against it is forfeited outright rather than deferred. */
  unrecoverable: boolean
}

export interface WashSaleAcquisition extends WashSaleWallet {
  date: string
  quantity: number
  /** The buy sits in the wallet being sold from — a wash sale, but a recoverable one. */
  same_wallet: boolean
}

/** Exposure only — no disallowed loss is rolled into replacement-share basis (issue #66). */
export interface WashSaleExposure {
  asset_id: string
  ticker: string | null
  asset_type: string | null
  /** False for asset classes the rule does not reach, such as crypto. */
  covered: boolean
  /** The selling wallet is Taxable, so a loss there is a deduction to lose. */
  reportable: boolean
  at_loss: boolean
  warning: boolean
  sell_date: string
  window_start: string
  window_end: string
  price: number | null
  average_price: number | null
  acquisitions: WashSaleAcquisition[]
  /** Every wallet the warning names — bought inside the window, or holds the instrument now. */
  wallets: WashSaleWallet[]
}

export interface MarketSymbolMatch {
  symbol: string
  name: string | null
  exchange: string | null
  quote_type: string | null
}

export interface MarketSymbolQuote {
  symbol: string
  name: string | null
  exchange: string | null
  currency: string
  price: number
  quote_type: string | null
}

export type TaxTreatment = 'taxable' | 'roth' | 'traditional' | 'hsa' | 'other'

export interface AssetGroup {
  id: string
  user_id: string
  name: string
  icon: string
  color: string
  position: number
  tax_treatment: TaxTreatment
  source: string
  connection_id: string | null
  institution_name: string | null
  // `type` of the provider account this wallet mirrors — what allocation by
  // account type groups on. Null for manual wallets.
  account_type: string | null
  asset_count: number
  current_value: number
  current_value_primary: number
}

/** Which way the money crossed the Wallet's boundary. `amount` is always
    positive; this carries the sign. */
export type ContributionKind = 'contribution' | 'distribution'

/** Whose money it was. Employer money is tracked apart because it is not the
    user's until it vests, and because a different annual limit applies. */
export type ContributionParty = 'self' | 'employer'

export interface AssetContribution {
  id: string
  group_id: string
  kind: ContributionKind
  party: ContributionParty
  amount: number
  date: string
  /** The year it counts against, which is not always `date`'s year. */
  tax_year: number
  vested_on: string | null
  /** Derived server-side against today, never stored. */
  is_vested: boolean
  source: string
  notes: string | null
}

export interface ContributionYear {
  tax_year: number
  own: number
  /** Gross, vested or not — annual limits are measured on gross. */
  employer: number
  distributions: number
  net: number
}

export interface ContributionSummary {
  group_id: string
  own_contributions: number
  employer_contributions: number
  employer_vested: number
  /** Outside `net`: not the user's money yet. */
  employer_unvested: number
  distributions: number
  /** CONTEXT.md's Net Contribution: own + employer_vested - distributions. */
  net: number
  return_net_of_contributions: number | null
  current_value: number | null
  years: ContributionYear[]
}

export interface ContributionImportRow {
  row_number: number
  /** Never null: the parser skips a row whose date, amount or direction it
      could not read, so a matched row has all three. */
  date: string
  tax_year: number
  kind: ContributionKind
  party: ContributionParty
  amount: number
  action: string
  /** The account the broker filed the row under, where the file names one. */
  account: string | null
  duplicate: boolean
}

export interface ContributionImportSkip {
  row_number: number
  action: string
  reason: string
}

/** A code, not a sentence — the server does not speak the user's language. */
export interface ContributionImportWarning {
  code: string
  count: number
}

export interface ContributionImportPreview {
  columns: string[]
  /** Every account the file names. More than one means a choice is required:
      an import writes into one wallet. */
  accounts: string[]
  total_rows: number
  matched: ContributionImportRow[]
  skipped: ContributionImportSkip[]
  warnings: ContributionImportWarning[]
}

export interface ContributionImportResult {
  import_id: string | null
  created: number
  duplicates: number
  skipped: number
}

export interface AssetValue {
  id: string
  asset_id: string
  amount: number
  date: string
  source: string
  recorded_at: string
}

export interface Goal {
  id: string
  user_id: string
  name: string
  target_amount: number
  current_amount: number
  currency: string
  target_amount_primary: number | null
  current_amount_primary: number | null
  target_date: string | null
  tracking_type: 'manual' | 'account' | 'asset' | 'asset_group' | 'net_worth'
  account_id: string | null
  asset_id: string | null
  asset_group_id: string | null
  status: 'active' | 'completed' | 'paused' | 'archived'
  icon: string | null
  color: string | null
  position: number
  metadata_json: Record<string, unknown> | null
  created_at: string
  updated_at: string
  percentage: number
  monthly_contribution: number | null
  on_track: 'ahead' | 'on_track' | 'behind' | 'overdue' | 'achieved' | null
  account_name: string | null
  asset_name: string | null
  asset_group_name: string | null
}

export interface GoalSummary {
  id: string
  name: string
  target_amount: number
  current_amount: number
  currency: string
  target_date: string | null
  status: string
  icon: string | null
  color: string | null
  percentage: number
  monthly_contribution: number | null
  on_track: string | null
}

export interface PaginatedResponse<T> {
  items: T[]
  total: number
  page: number
  limit: number
}

// Income / expense / net totals for all transactions matching the active
// filters (issue #185) — accompanies the paginated /transactions response.
export interface TransactionsSummary {
  income: number
  expense: number
  net: number
  // Absolute total of everything excluded from income/expense for the same
  // rows — transfers, treat_as_transfer categories and ignored items (#242).
  excluded: number
  currency: string
}

export interface PaginatedTransactions extends PaginatedResponse<Transaction> {
  summary?: TransactionsSummary
}

// Reports (universal schema for all report types)
export interface ReportBreakdown {
  key: string
  label: string
  value: number
  color: string
}

export interface ReportSummary {
  primary_value: number
  change_amount: number
  change_percent: number | null
  breakdowns: ReportBreakdown[]
}

export interface ReportDataPoint {
  date: string
  value: number
  breakdowns: Record<string, number>
  change: number | null
  composition?: ReportCompositionItem[]
}

export interface ReportMeta {
  type: string
  series_keys: string[]
  currency: string
  interval: string
  forecast_start_date?: string | null
  baseline_active?: boolean
  baseline_lookback_days?: number | null
}

export interface ReportCompositionItem {
  key: string
  label: string
  value: number
  color: string
  group: string
}

export interface CategoryTrendItem {
  key: string
  label: string
  color: string
  total: number
  group: string
  series: ReportDataPoint[]
}

export interface Attachment {
  id: string
  transaction_id: string
  filename: string
  content_type: string
  size: number
  created_at: string
}

export interface ReportResponse {
  summary: ReportSummary
  trend: ReportDataPoint[]
  meta: ReportMeta
  composition: ReportCompositionItem[]
  category_trend: CategoryTrendItem[]
}
