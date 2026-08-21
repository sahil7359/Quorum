"use client";

import { useEffect, useRef } from "react";

import type { LogLine } from "@/hooks/useReview";

function clock(t: number): string {
  return new Date(t).toLocaleTimeString([], { hour12: false });
}

export function EventLog({ log }: { log: LogLine[] }) {
  const endRef = useRef<HTMLDivElement>(null);

  // Autoscroll to the newest line as the review streams -- a log console that doesn't follow
  // its own tail makes the reader chase it.
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [log]);

  return (
    <div className="rounded-lg border border-white/10 bg-black/30 p-3">
      <p className="mb-2 text-xs font-medium opacity-50">Process log</p>
      <div className="max-h-56 overflow-y-auto font-mono text-xs leading-relaxed">
        {log.length === 0 ? (
          <p className="opacity-30">waiting for events…</p>
        ) : (
          log.map((line, i) => (
            <div key={i} className="flex gap-2">
              <span className="shrink-0 opacity-30">{clock(line.t)}</span>
              <span className="shrink-0 w-40 truncate text-sky-300/70">{line.event}</span>
              <span className="opacity-80">{line.detail}</span>
            </div>
          ))
        )}
        <div ref={endRef} />
      </div>
    </div>
  );
}
