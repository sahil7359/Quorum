import { SeverityBadge } from "@/components/SeverityBadge";
import type { Finding } from "@/lib/types";

export function FindingCard({ finding }: { finding: Finding }) {
  return (
    <li className="rounded-lg border border-white/10 p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-baseline gap-2">
          <span className="text-xs font-medium uppercase tracking-wide opacity-50">
            {finding.specialist.replace("_", " ")}
          </span>
          <h3 className="text-sm font-semibold">{finding.title}</h3>
        </div>
        <SeverityBadge severity={finding.severity} />
      </div>
      <p className="mt-2 text-sm opacity-80">{finding.body}</p>
      <p className="mt-3 text-xs opacity-50">
        {finding.file_path}
        {finding.line_start !== null ? `:${finding.line_start}` : ""} — cites{" "}
        {finding.citation.display}
      </p>
    </li>
  );
}
