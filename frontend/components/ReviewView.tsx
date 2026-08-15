"use client";

import { useState } from "react";

import { FindingCard } from "@/components/FindingCard";
import { ProgressSteps } from "@/components/ProgressSteps";
import { useReview } from "@/hooks/useReview";

const EXAMPLES = [
  { label: "mypy #21647", repo: "python/mypy", pr: 21647 },
  { label: "black #5237", repo: "psf/black", pr: 5237 },
];

export function ReviewView() {
  const { state, run, reset } = useReview();
  const [repo, setRepo] = useState("");
  const [prNumber, setPrNumber] = useState("");

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

  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col gap-8 p-6">
      <header>
        <h1 className="text-2xl font-semibold">Quorum</h1>
        <p className="text-sm opacity-60">
          Reviews a real pull request — citation-backed findings, or none, never invented.
        </p>
      </header>

      <form onSubmit={submit} className="flex flex-col gap-3">
        <div className="flex gap-2">
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
        </div>

        {state.phase === "idle" && (
          <div className="flex flex-wrap gap-2 text-xs opacity-70">
            <span className="pt-1">Try:</span>
            {EXAMPLES.map((ex) => (
              <button
                key={ex.label}
                type="button"
                onClick={() => runExample(ex.repo, ex.pr)}
                className="rounded-full border border-white/10 px-2 py-1 transition hover:bg-white/5"
              >
                {ex.label}
              </button>
            ))}
          </div>
        )}
      </form>

      {state.phase !== "idle" && (
        <section className="flex flex-col gap-6">
          <div className="rounded-lg border border-white/10 p-4">
            <ProgressSteps state={state} />
          </div>

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
              Start another review
            </button>
          )}
        </section>
      )}
    </main>
  );
}
