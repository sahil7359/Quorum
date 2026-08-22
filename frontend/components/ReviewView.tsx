"use client";

import { useCallback, useEffect, useState } from "react";

import { EventLog } from "@/components/EventLog";
import { FindingCard } from "@/components/FindingCard";
import { HistoryPanel } from "@/components/HistoryPanel";
import { ProgressSteps } from "@/components/ProgressSteps";
import { StatusBar } from "@/components/StatusBar";
import { useReview } from "@/hooks/useReview";
import { fetchRecentReviews } from "@/lib/api";
import type { ReviewCompleted } from "@/lib/types";

// Curated demo PRs -- real merged pull requests on repos with real documentation.
//
// The first is already indexed and cached, so on the hosted (memory-constrained) demo it
// returns instantly and reliably. The second is a full first-time review that indexes the
// repo's docs live -- it's the richer showcase (real findings with citations), and it runs
// great locally or on a host with adequate memory; on the free 512MB tier it may exceed memory
// while indexing and stop early (the UI says so rather than hanging). See DEMO.md.
const EXAMPLES = [
  {
    label: "psf/black #5280",
    repo: "psf/black",
    pr: 5280,
    blurb: "already indexed — returns instantly (best for the hosted demo)",
  },
  {
    label: "python/mypy #21647",
    repo: "python/mypy",
    pr: 21647,
    blurb: "full first-time review, 5 findings — best run locally / with adequate memory",
  },
];

export function ReviewView() {
  const [history, setHistory] = useState<ReviewCompleted[]>([]);
  const refreshHistory = useCallback(() => {
    void fetchRecentReviews(20).then(setHistory);
  }, []);

  const { state, run, reset } = useReview(refreshHistory);
  const [repo, setRepo] = useState("");
  const [prNumber, setPrNumber] = useState("");

  useEffect(() => refreshHistory(), [refreshHistory]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const n = Number.parseInt(prNumber, 10);
    if (!repo.includes("/") || Number.isNaN(n) || n <= 0) return;
    run(repo.trim(), n);
  };

  const runExample = (exampleRepo: string, pr: number) => {
    setRepo(exampleRepo);
    setPrNumber(String(pr));
    run(exampleRepo, pr);
  };

  const busy = state.phase === "streaming";
  const active = state.phase !== "idle";

  return (
    <main className="mx-auto flex min-h-screen max-w-5xl flex-col gap-6 p-6">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Quorum</h1>
          <p className="text-sm opacity-60">
            Reviews a real pull request — citation-backed findings, or none, never invented.
          </p>
        </div>
        <StatusBar />
      </header>

      <div className="grid gap-6 lg:grid-cols-[1.4fr_1fr]">
        {/* Left column: run a review + live activity */}
        <section className="flex flex-col gap-4">
          <div className="rounded-xl border border-white/10 bg-white/[0.03] p-4">
            <h2 className="text-sm font-medium">Run a review</h2>
            <p className="mt-0.5 text-xs opacity-50">
              One click on an example, or any public PR. First run of a PR indexes its docs
              (~30s); after that it&apos;s cached and instant.
            </p>

            <div className="mt-3 grid gap-2 sm:grid-cols-2">
              {EXAMPLES.map((ex) => (
                <button
                  key={ex.label}
                  type="button"
                  disabled={busy}
                  onClick={() => runExample(ex.repo, ex.pr)}
                  className="rounded-lg border border-white/10 p-3 text-left transition hover:bg-white/5 disabled:opacity-50"
                >
                  <span className="text-sm font-medium">{ex.label}</span>
                  <span className="mt-1 block text-xs opacity-60">{ex.blurb}</span>
                </button>
              ))}
            </div>

            <form onSubmit={submit} className="mt-3 flex gap-2">
              <input
                value={repo}
                onChange={(e) => setRepo(e.target.value)}
                placeholder="owner/repo"
                disabled={busy}
                className="flex-1 rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-sm outline-none focus:border-white/30 disabled:opacity-50"
              />
              <input
                value={prNumber}
                onChange={(e) => setPrNumber(e.target.value)}
                placeholder="PR #"
                inputMode="numeric"
                disabled={busy}
                className="w-24 rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-sm outline-none focus:border-white/30 disabled:opacity-50"
              />
              <button
                type="submit"
                disabled={busy}
                className="rounded-lg border border-white/10 bg-white/10 px-4 py-2 text-sm font-medium transition hover:bg-white/15 disabled:opacity-50"
              >
                {busy ? "Reviewing…" : "Review"}
              </button>
            </form>
          </div>

          {active && (
            <div className="flex flex-col gap-4">
              <div className="rounded-lg border border-white/10 p-4">
                <ProgressSteps state={state} />
              </div>

              <EventLog log={state.log} />

              {state.phase === "error" && (
                <p className="rounded-lg border border-red-400/30 bg-red-400/10 p-4 text-sm text-red-300">
                  {state.error}
                </p>
              )}

              {state.result && (
                <div className="flex flex-col gap-4">
                  {state.result.error && (
                    <p className="rounded-lg border border-amber-400/30 bg-amber-400/10 p-4 text-sm text-amber-300">
                      {state.result.error}
                    </p>
                  )}
                  {state.result.diff_truncated && (
                    <p className="text-xs opacity-50">
                      Note: this diff was too large and was truncated before review.
                    </p>
                  )}
                  {state.result.findings.length === 0 && !state.result.error ? (
                    <p className="text-sm opacity-60">
                      No findings survived citation checks — either nothing worth flagging, or
                      nothing the specialists could ground in the repo&apos;s own docs.
                    </p>
                  ) : (
                    <ul className="flex flex-col gap-3">
                      {state.result.findings.map((f) => (
                        <FindingCard key={f.finding_id} finding={f} />
                      ))}
                    </ul>
                  )}
                </div>
              )}

              {(state.phase === "done" || state.phase === "error") && (
                <button
                  type="button"
                  onClick={reset}
                  className="self-start text-xs underline opacity-60 hover:opacity-100"
                >
                  Clear
                </button>
              )}
            </div>
          )}
        </section>

        {/* Right column: history */}
        <section className="flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-medium">Recent reviews</h2>
            <button
              type="button"
              onClick={refreshHistory}
              className="text-xs opacity-50 underline hover:opacity-100"
            >
              refresh
            </button>
          </div>
          <HistoryPanel reviews={history} />
        </section>
      </div>
    </main>
  );
}
