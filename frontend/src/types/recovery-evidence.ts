import type { EvidenceObservation } from './investment-evidence'

export type RecoveryRole = 'allowed_claim' | 'platform_ledger' | 'recovery_notice' | 'receiving_receipt' | 'disposition' | 'cash_proceeds' | 'equity_statement' | 'tax_workpaper';
export type RecoveryState = 'confirmed' | 'candidate' | 'conflict' | 'missing';
export type AssertionStatus = 'reported' | 'modeled' | 'unverified' | 'supported' | 'conflict' | 'missing';
export type RecoveryDetails = {
  account_bucket: string | null; // source Earn/Custody/BIA/wallet label, never destination
  boundary_kind: string | null; // source-stated boundary only
  claim_amount: string | null;
  claim_currency: string | null;
  proceeds: string | null;
  proceeds_currency: string | null;
  cash_credited: string | null;
  cash_currency: string | null;
  acquisition_date: string | null; // ISO date when supported
  statement_date: string | null;
  reported_cost: string | null;
  reported_cost_currency: string | null;
  provisional_allocation: string | null;
  allocation_currency: string | null;
};
// Source/source account/locator/quantity/asset/value/fee/raw date/time precision
// are existing EvidenceObservation and EvidenceLegInput fields.
export type RecoveryEntryInput = {
  key: string; // stable client/source annotation identity, retain on retry
  observation_id?: string | null; // attach retained observation OR supply observation
  observation?: EvidenceObservation | null;
  leg_key: string;
  case_key: string;
  round_key: string | null;
  round_asset_key: string | null;
  role: RecoveryRole;
  reported_state: RecoveryState;
  details: RecoveryDetails;
  missing_evidence: string[];
};
export type RecoveryApplication = {
  leg_id: string | null;
  application_id: string | null;
  status: 'unapplied' | 'applied' | 'reversed' | 'unsupported';
  reason_codes: string[];
};
export type RecoveryEntryRead = Omit<RecoveryEntryInput, 'observation_id' | 'observation'> & {
  source_group_id: string | null;
  source_group_name: string | null;
  id: string | null; // null only for unsaved preview
  observation_id: string | null;
  observation: EvidenceObservation;
  application: RecoveryApplication;
  associated_holding?: {
    asset_id: string;
    group_id: string | null;
    name: string | null;
    asset_symbol: string | null;
    source: string | null;
    stored_quantity: string | null;
    quantity_observed_at: string | null;
    reason_codes: string[];
  } | null;
  reason_codes: string[];
};
export type RecoveryReviewInput = {
  key: string; // stable client review identity
  kind: 'relation' | 'assertion' | 'correction' | 'allocation';
  entry_id: string;
  target_entry_id: string | null;
  supersedes_id: string | null;
  relation_kind: 'claim_notice' | 'notice_receipt' | 'receipt_disposition' | 'disposition_proceeds' | 'candidate_acquisition' | 'owned_transfer_reference' | 'documentary_equity_distribution' | 'documentary_receipt_disposition' | null;
  relation_state: RecoveryState | null;
  assertion_kind: 'reported_cost' | 'provisional_allocation' | 'valuation' | 'account_mapping' | 'lot_mapping' | 'accounting_assumption' | 'filing_assertion' | null;
  assertion_status: AssertionStatus | null;
  value: string | null;
  currency: string | null;
  field: string | null; // correction field/reference or named assumption
  proposed_value: string | null;
  source_locator: string;
  reason: string;
  supporting_observation_ids: string[];
  required_entry_ids: string[];
  required_review_ids: string[];
  missing_evidence: string[];
  conflicting_fields: string[];
  owned_transfer_id: string | null;
  account_mapping_evidence: string | null;
  timing_evidence: string | null;
  quantity_adjustment: string | null; // explicit signed decimal, never inferred
  adjustment_evidence: string | null;
  documentary_quantity?: string | null; // Association only, never inventory or lot allocation.
};
export type RecoveryReviewRead = RecoveryReviewInput & {
  id: string;
  created_at: string;
  created_by: string | null;
  is_current: boolean;
  blockers: string[];
  ready_for_review: boolean;
};
export type RecoveryPackage = {
  workspace_id: string;
  group_id: string;
  revision: string;
  entries: RecoveryEntryRead[];
  reviews: RecoveryReviewRead[];
  round_count: number;
  asset_record_count: number;
  missing_evidence: string[];
  allocation_blockers: string[];
  coverage: string[];
};

export const RECOVERY_ROLES = ['allowed_claim', 'platform_ledger', 'recovery_notice', 'receiving_receipt', 'disposition', 'cash_proceeds', 'equity_statement', 'tax_workpaper'] as const
