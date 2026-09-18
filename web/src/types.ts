/** Shapes returned by the Seekr read API. Only the fields the UI reads are
 *  named; everything else on a response is ignored rather than mistyped. */

export interface Attribute {
  attribute_type: string;
  value: string;
  evidence_count: number;
  sources: string[];
}

/** Why a person ranks where they do. `/v1/query` attaches this; the faceted
 *  `/v1/persons` endpoint stays deliberately unordered and omits it. */
export interface ScoreComponents {
  depth?: number;
  output?: number;
  confidence?: number;
  recency?: number;
  corroboration?: number;
  breadth?: number;
  [signal: string]: number | undefined;
}

export interface MatchedEvidence {
  attribute_type: string;
  value: string;
  source?: string | null;
  confidence?: number | null;
}

export interface PersonSummary {
  id: string;
  canonical_name?: string | null;
  current_role?: string | null;
  current_organization?: string | null;
  location?: string | null;
  country?: string | null;
  aliases?: string[];
  profile_urls?: string[];
  updated_at?: string | null;
  merged_into?: string | null;
  attributes?: Attribute[];
  organizations?: string[];
  matched_organization?: string | null;
  from_live_search?: boolean;
  score?: number | null;
  score_components?: ScoreComponents | null;
  matched_evidence?: MatchedEvidence[] | null;
  /** "partial" rows meet some of the query, and name what they miss */
  match?: "full" | "partial";
  missing?: NotFoundTerm[];
}

export interface AppliedFilters {
  /** the subjects asked for, as typed; `skills` is what they resolved to */
  subjects?: string[];
  skills?: string[];
  skill_patterns?: string[];
  organizations?: string[];
  locations?: string[];
  countries?: string[];
  name_terms?: string[];
  roles?: string[];
  limit?: number;
  offset?: number;
}

export interface Correction {
  typed: string;
  matched: string;
}

/** What was searched in place of the words typed ("physicists" -> physics). */
export interface Rewrite {
  typed: string;
  searched: string | string[];
  how?: string;
}

/** A word asking to select people by a protected attribute: never applied. */
export interface ProtectedTerm {
  term: string;
  attribute: string;
}

/** "not at Google": what was left out, and as what kind of constraint. */
export interface Exclusion {
  term: string;
  as: string[];
}

/** The parts of a question that are not subject filters. */
export interface QueryUnderstanding {
  rewrites?: Rewrite[];
  protected_terms?: ProtectedTerm[];
  exclusions?: Exclusion[];
  require_all_orgs?: boolean;
  min_publications?: number | null;
  min_citations?: number | null;
}

export interface FilterAlone {
  filter: string;
  value: string | number | boolean;
  matches: number | null;
}

export interface EmptyReason {
  message: string;
  each_filter_alone?: FilterAlone[];
}

export interface DiscoverySuggestion {
  source: string;
  external_id: string;
  name?: string | null;
  affiliation?: string | null;
  role?: string | null;
  location?: string | null;
}

export interface NotFoundTerm {
  term: string;
  as: string;
}

export interface AppliedClause {
  term: string;
  as: string;
}

export interface QueryResponse extends QueryUnderstanding {
  query: string;
  applied_filters?: AppliedFilters | null;
  applied_clauses?: AppliedClause[];
  not_found?: NotFoundTerm[];
  unmatched_terms?: string[];
  corrections?: Correction[];
  count: number;
  total_matches?: number;
  has_more?: boolean;
  next_offset?: number | null;
  results: PersonSummary[];
  matched_nothing?: boolean;
  explanation?: string | null;
  empty_reason?: EmptyReason | null;
  discover_available?: boolean;
  storage?: "read-only" | "writable";
  discovery_suggestions?: DiscoverySuggestion[];
  stored_from_live?: number;
  replayed_from_cache?: number;
}

export interface FacetValue {
  value: string;
  people: number;
}

export interface FacetResponse {
  field: string;
  values: FacetValue[];
}

export interface Affiliation {
  organization: string;
  relation: string;
  role?: string | null;
  start_date?: string | null;
  end_date?: string | null;
  is_current?: boolean;
}

export interface Publication {
  title: string;
  venue?: string | null;
  published_date?: string | null;
  url?: string | null;
  citations?: number | null;
}

export interface Project {
  name: string;
  url?: string | null;
  technologies?: string[];
  activity?: Record<string, number | string>;
  last_active_at?: string | null;
}

export interface Conflict {
  attribute: string;
  status: string;
  side_a: { value: string; source?: string | null };
  side_b: { value: string; source?: string | null };
}

export interface ProvenanceSource {
  source: string;
  external_id: string;
  url?: string | null;
  match_method?: string | null;
  match_signals?: { reason?: string } | null;
  review_state?: string | null;
  last_observed?: string | null;
}

export interface GraphNode {
  id: string;
  label: string;
  type: "organization" | "person" | string;
}

export interface GraphEdge {
  from: string;
  to: string;
  type: string;
  shared_publications?: number;
}

export interface DocumentLink {
  url: string;
  kind?: string;
  evidence?: string | null;
  found_on?: string | null;
}

export interface Shortlist {
  id: number;
  name: string;
  count?: number;
}

export interface ShortlistMember {
  person_id: string;
  canonical_name?: string | null;
  found_by_query?: string | null;
  added_at?: string | null;
}

export interface ShortlistDetail extends Shortlist {
  members: ShortlistMember[];
}

export interface SourceHealth {
  source: string;
  status: string;
  runs: number;
  last_finished_at?: string | null;
}

export interface WebhookHealth {
  active_subscriptions: number;
  pending: number;
  delivered: number;
  failed: number;
}

export interface DuplicateCandidate {
  candidate_id: number;
  person_id: string;
  person_name: string;
  duplicate_person_id: string;
  duplicate_person_name: string;
  score?: number;
  signals?: { reason?: string } | null;
}

export interface FuzzyMerge {
  link_id: number;
  person_id: string;
  person_name: string;
  source: string;
  external_id: string;
  record_name?: string | null;
  match_method?: string | null;
  signals?: { reason?: string } | null;
}
