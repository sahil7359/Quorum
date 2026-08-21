"use client";

import { useEffect, useState } from "react";

import { fetchStatus } from "@/lib/api";
import type { Status } from "@/lib/types";

function Pill({ label, value, tone = "default" }: { label: string; value: string; tone?: "default" | "good" | "warn" }) {
  const toneClass =
    tone === "good"
      ? "border-emerald-400/30 text-emerald-300"
      : tone === "warn"
        ? "border-amber-400/30 text-amber-300"
        : "border-white/10 text-white/70";
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs ${toneClass}`}>
      <span className="opacity-50">{label}</span>
      <span className="font-medium">{value}</span>
    </span>
  );
}

export function StatusBar() {
  const [status, setStatus] = useState<Status | null>(null);
  const [reachable, setReachable] = useState<boolean | null>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      const s = await fetchStatus();
      if (!alive) return;
      setStatus(s);
      setReachable(s !== null);
    };
    void load();
    const id = setInterval(() => void load(), 15000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const budget = status?.budget;
  const budgetValue =
    budget && budget.limit !== null && budget.consumed !== null
      ? `${budget.consumed.toLocaleString()} / ${budget.limit.toLocaleString()} tokens`
      : "—";

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Pill
        label="backend"
        value={reachable === null ? "…" : reachable ? "online" : "unreachable"}
        tone={reachable ? "good" : reachable === false ? "warn" : "default"}
      />
      {status && <Pill label="model" value={`${status.provider} · ${status.model}`} />}
      <Pill
        label="daily budget"
        value={budgetValue}
        tone={budget?.exhausted ? "warn" : "default"}
      />
    </div>
  );
}
