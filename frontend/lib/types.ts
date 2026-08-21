export type Severity = "info" | "low" | "medium" | "high";
export type SpecialistKind = "correctness" | "security" | "test_coverage";
export type RunStatus = "running" | "proposed" | "published" | "rejected" | "failed";

export interface Citation {
  chunk_id: string;
  file_path: string;
  section_path: string;
  start_line: number | null;
  byte_range: [number, number];
  display: string;
}

export interface Finding {
  finding_id: string;
  specialist: SpecialistKind;
  severity: Severity;
  confidence: number;
  title: string;
  body: string;
  file_path: string;
  line_start: number | null;
  citation: Citation;
}

export interface ReviewCompleted {
  run_id: string;
  repo: string;
  pr_number: number;
  head_sha: string;
  status: RunStatus;
  findings: Finding[];
  diff_truncated: boolean;
  error: string | null;
  posted_to_github: boolean;
}

export interface Status {
  provider: string;
  model: string;
  config_hash: string;
  budget: {
    consumed: number | null;
    limit: number | null;
    exhausted: boolean | null;
  };
}

export interface IngestionStarted {
  repo: string;
  commit_sha: string;
}

export interface IngestionCompleted {
  chunks: number;
}

export interface NodeIngest {
  files_changed: number;
  diff_truncated: boolean;
  scoping_reduction_pct: number;
}

export interface NodeRoute {
  specialists: SpecialistKind[];
  reason: string;
  heuristic_floor: SpecialistKind[];
  llm_added: SpecialistKind[];
}

export interface NodeSpecialists {
  candidates_proposed: number;
  failed_specialists: string[];
}

export interface NodeSynthesise {
  findings: Finding[];
  dropped: string[];
}

// The full set of event names the backend actually emits, tagged so a
// consumer can narrow on `event` and get the right payload type -- one
// source of truth for "what can arrive on this stream", matching
// review_service.py's review_stream and _node_event exactly.
export type ReviewEvent =
  | { event: "ingestion.started"; data: IngestionStarted }
  | { event: "ingestion.completed"; data: IngestionCompleted }
  | { event: "node.ingest"; data: NodeIngest }
  | { event: "node.route"; data: NodeRoute }
  | { event: "node.specialists"; data: NodeSpecialists }
  | { event: "node.synthesise"; data: NodeSynthesise }
  | { event: "review.completed"; data: ReviewCompleted };
