import type { Severity } from "@/lib/types";

const STYLES: Record<Severity, string> = {
  high: "border-red-400/30 bg-red-400/10 text-red-300",
  medium: "border-amber-400/30 bg-amber-400/10 text-amber-300",
  low: "border-sky-400/30 bg-sky-400/10 text-sky-300",
  info: "border-white/20 bg-white/5 text-white/70",
};

export function SeverityBadge({ severity }: { severity: Severity }) {
  return (
    <span
      className={`shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide ${STYLES[severity]}`}
    >
      {severity}
    </span>
  );
}
