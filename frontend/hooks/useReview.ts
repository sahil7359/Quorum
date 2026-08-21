"use client";

import { useCallback, useRef, useState } from "react";

import { streamReview } from "@/lib/api";
import type {
  IngestionCompleted,
  IngestionStarted,
  NodeIngest,
  NodeRoute,
  NodeSpecialists,
  ReviewCompleted,
  ReviewEvent,
} from "@/lib/types";

export type ReviewPhase = "idle" | "streaming" | "done" | "error";

export interface LogLine {
  t: number;
  event: string;
  detail: string;
}

export interface ReviewState {
  phase: ReviewPhase;
  ingestionStarted: IngestionStarted | null;
  ingestionCompleted: IngestionCompleted | null;
  ingest: NodeIngest | null;
  route: NodeRoute | null;
  specialists: NodeSpecialists | null;
  result: ReviewCompleted | null;
  error: string | null;
  log: LogLine[];
}

const INITIAL_STATE: ReviewState = {
  phase: "idle",
  ingestionStarted: null,
  ingestionCompleted: null,
  ingest: null,
  route: null,
  specialists: null,
  result: null,
  error: null,
  log: [],
};

// A one-line, human-readable summary of each raw SSE event, for the process-log console. This
// is the "what is the agent doing right now" narration, distinct from the structured state the
// rest of the dashboard renders from.
function describe(evt: ReviewEvent): string {
  switch (evt.event) {
    case "ingestion.started":
      return `indexing docs for ${evt.data.repo} @ ${evt.data.commit_sha.slice(0, 8)}`;
    case "ingestion.completed":
      return `docs indexed — ${evt.data.chunks} chunks`;
    case "node.ingest":
      return `diff read — ${evt.data.files_changed} files, context scoped ${evt.data.scoping_reduction_pct.toFixed(0)}%`;
    case "node.route":
      return `routed to: ${evt.data.specialists.join(", ")}${
        evt.data.llm_added.length ? ` (model added ${evt.data.llm_added.join(", ")})` : ""
      }`;
    case "node.specialists":
      return `specialists done — ${evt.data.candidates_proposed} candidate finding(s)`;
    case "node.synthesise":
      return `grounding — ${evt.data.findings.length} kept, ${evt.data.dropped.length} dropped`;
    case "review.completed":
      return `review complete — ${evt.data.findings.length} finding(s), status ${evt.data.status}`;
    default:
      return "";
  }
}

export function useReview(onComplete?: () => void) {
  const [state, setState] = useState<ReviewState>(INITIAL_STATE);
  const controllerRef = useRef<AbortController | null>(null);

  const run = useCallback(
    (repo: string, prNumber: number) => {
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;

      setState({
        ...INITIAL_STATE,
        phase: "streaming",
        log: [{ t: Date.now(), event: "request", detail: `POST review ${repo}#${prNumber}` }],
      });

      void (async () => {
        try {
          for await (const evt of streamReview(repo, prNumber, controller.signal)) {
            setState((prev) => {
              const line: LogLine = { t: Date.now(), event: evt.event, detail: describe(evt) };
              const log = line.detail ? [...prev.log, line] : prev.log;
              switch (evt.event) {
                case "ingestion.started":
                  return { ...prev, log, ingestionStarted: evt.data };
                case "ingestion.completed":
                  return { ...prev, log, ingestionCompleted: evt.data };
                case "node.ingest":
                  return { ...prev, log, ingest: evt.data };
                case "node.route":
                  return { ...prev, log, route: evt.data };
                case "node.specialists":
                  return { ...prev, log, specialists: evt.data };
                case "node.synthesise":
                  return { ...prev, log };
                case "review.completed":
                  return { ...prev, log, phase: "done", result: evt.data };
                default:
                  return { ...prev, log };
              }
            });
          }
          onComplete?.();
        } catch (err) {
          if (controller.signal.aborted) return;
          setState((prev) => ({
            ...prev,
            phase: "error",
            error: err instanceof Error ? err.message : String(err),
            log: [
              ...prev.log,
              {
                t: Date.now(),
                event: "error",
                detail: err instanceof Error ? err.message : String(err),
              },
            ],
          }));
        }
      })();
    },
    [onComplete],
  );

  const reset = useCallback(() => {
    controllerRef.current?.abort();
    setState(INITIAL_STATE);
  }, []);

  return { state, run, reset };
}
