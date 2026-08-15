import type { ReviewState } from "@/hooks/useReview";

interface Step {
  key: string;
  label: string;
  detail: string | null;
  done: boolean;
}

function buildSteps(state: ReviewState): Step[] {
  const steps: Step[] = [];

  if (state.ingestionStarted) {
    steps.push({
      key: "ingestion",
      label: "Indexing repository docs",
      detail: state.ingestionCompleted
        ? `${state.ingestionCompleted.chunks} chunks ready`
        : "first review of this commit — fetching and chunking docs",
      done: state.ingestionCompleted !== null,
    });
  }

  steps.push({
    key: "ingest",
    label: "Reading the diff",
    detail: state.ingest
      ? `${state.ingest.files_changed} files changed, context scoped down ${state.ingest.scoping_reduction_pct.toFixed(0)}%`
      : null,
    done: state.ingest !== null,
  });

  steps.push({
    key: "route",
    label: "Deciding which specialists to run",
    detail: state.route ? state.route.specialists.join(", ") : null,
    done: state.route !== null,
  });

  steps.push({
    key: "specialists",
    label: "Running specialists",
    detail: state.specialists
      ? `${state.specialists.candidates_proposed} candidate finding(s) proposed`
      : null,
    done: state.specialists !== null,
  });

  steps.push({
    key: "synthesise",
    label: "Grounding and finalising",
    detail: state.result ? `${state.result.findings.length} finding(s) survived citation checks` : null,
    done: state.result !== null,
  });

  return steps;
}

export function ProgressSteps({ state }: { state: ReviewState }) {
  const steps = buildSteps(state);
  const activeIndex = steps.findIndex((s) => !s.done);

  return (
    <ol className="flex flex-col gap-3">
      {steps.map((step, i) => {
        const active = i === activeIndex;
        return (
          <li key={step.key} className="flex items-start gap-3">
            <span
              className={`mt-0.5 h-2 w-2 shrink-0 rounded-full ${
                step.done ? "bg-emerald-400" : active ? "animate-pulse bg-sky-400" : "bg-white/15"
              }`}
            />
            <div>
              <p className={`text-sm ${step.done || active ? "opacity-100" : "opacity-40"}`}>
                {step.label}
              </p>
              {step.detail && <p className="text-xs opacity-50">{step.detail}</p>}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
