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
} from "@/lib/types";

export type ReviewPhase = "idle" | "streaming" | "done" | "error";

export interface ReviewState {
  phase: ReviewPhase;
  ingestionStarted: IngestionStarted | null;
  ingestionCompleted: IngestionCompleted | null;
  ingest: NodeIngest | null;
  route: NodeRoute | null;
  specialists: NodeSpecialists | null;
  result: ReviewCompleted | null;
  error: string | null;
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
};

export function useReview() {
  const [state, setState] = useState<ReviewState>(INITIAL_STATE);
  const controllerRef = useRef<AbortController | null>(null);

  const run = useCallback((repo: string, prNumber: number) => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;

    setState({ ...INITIAL_STATE, phase: "streaming" });

    void (async () => {
      try {
        for await (const evt of streamReview(repo, prNumber, controller.signal)) {
          setState((prev) => {
            switch (evt.event) {
              case "ingestion.started":
                return { ...prev, ingestionStarted: evt.data };
              case "ingestion.completed":
                return { ...prev, ingestionCompleted: evt.data };
              case "node.ingest":
                return { ...prev, ingest: evt.data };
              case "node.route":
                return { ...prev, route: evt.data };
              case "node.specialists":
                return { ...prev, specialists: evt.data };
              case "node.synthesise":
                return prev;
              case "review.completed":
                return { ...prev, phase: "done", result: evt.data };
              default:
                return prev;
            }
          });
        }
      } catch (err) {
        if (controller.signal.aborted) return;
        setState((prev) => ({
          ...prev,
          phase: "error",
          error: err instanceof Error ? err.message : String(err),
        }));
      }
    })();
  }, []);

  const reset = useCallback(() => {
    controllerRef.current?.abort();
    setState(INITIAL_STATE);
  }, []);

  return { state, run, reset };
}
