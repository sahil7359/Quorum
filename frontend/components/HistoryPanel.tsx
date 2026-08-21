"use client";

import { useState } from "react";

import { FindingCard } from "@/components/FindingCard";
import { SeverityBadge } from "@/components/SeverityBadge";
import type { ReviewCompleted } from "@/lib/types";

function topSeverity(r: ReviewCompleted) {
  const order = { high: 3, medium: 2, low: 1, info: 0 } as const;
  return r.findings.reduce<ReviewCompleted["findings"][number] | null>((best, f) => {
    if (!best || order[f.severity] > order[best.severity]) return f;
    return best;
  }, null);
}

function Row({ review }: { review: ReviewCompleted }) {
  const [open, setOpen] = useState(false);
  const top = topSeverity(review);
  return (
    <li className="rounded-lg border border-white/10">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between gap-3 p-3 text-left transition hover:bg-white/5"
      >
        <span className="min-w-0">
          <span className="text-sm font-medium">
            {review.repo}#{review.pr_number}
          </span>
          <span className="ml-2 text-xs opacity-50">
            {review.findings.length} finding{review.findings.length === 1 ? "" : "s"}
            {review.posted_to_github ? " · posted" : ""}
          </span>
        </span>
        {top ? <SeverityBadge severity={top.severity} /> : <span className="text-xs opacity-40">clean</span>}
      </button>
      {open && review.findings.length > 0 && (
        <ul className="flex flex-col gap-2 border-t border-white/10 p-3">
          {review.findings.map((f) => (
            <FindingCard key={f.finding_id} finding={f} />
          ))}
        </ul>
      )}
      {open && review.findings.length === 0 && (
        <p className="border-t border-white/10 p-3 text-xs opacity-50">
          No findings survived citation checks.
        </p>
      )}
    </li>
  );
}

export function HistoryPanel({ reviews }: { reviews: ReviewCompleted[] }) {
  if (reviews.length === 0) {
    return (
      <p className="text-xs opacity-40">
        No reviews yet. Run one above and it appears here (cached, so it&apos;s instant next time).
      </p>
    );
  }
  return (
    <ul className="flex flex-col gap-2">
      {reviews.map((r) => (
        <Row key={`${r.run_id}`} review={r} />
      ))}
    </ul>
  );
}
