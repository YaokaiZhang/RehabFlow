import { ButtonHTMLAttributes, HTMLAttributes, ReactNode } from "react";

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

type AppButtonVariant = "primary" | "secondary" | "ghost" | "danger";

type AppButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: AppButtonVariant;
};

const buttonClasses: Record<AppButtonVariant, string> = {
  primary: "border-emerald-700 bg-emerald-600 text-white shadow-[0_2px_0_#047857] hover:bg-emerald-700",
  secondary: "border-emerald-200 bg-emerald-50 text-emerald-800 shadow-[0_2px_0_#a7f3d0] hover:bg-emerald-100",
  ghost: "border-transparent bg-transparent text-slate-700 hover:bg-slate-100",
  danger: "border-rose-700 bg-rose-600 text-white shadow-[0_2px_0_#be123c] hover:bg-rose-700",
};

export function AppButton({ variant = "primary", className, type = "button", ...props }: AppButtonProps) {
  return (
    <button
      type={type}
      className={cx(
        "inline-flex min-h-10 items-center justify-center rounded-md border px-4 py-2 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-50",
        buttonClasses[variant],
        className,
      )}
      {...props}
    />
  );
}

type StatusTone = "success" | "info" | "attention" | "risk" | "neutral";

const statusClasses: Record<StatusTone, string> = {
  success: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  info: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  attention: "bg-amber-50 text-amber-700 ring-amber-200",
  risk: "bg-rose-50 text-rose-700 ring-rose-200",
  neutral: "bg-slate-100 text-slate-700 ring-slate-200",
};

export function StatusBadge({ tone = "neutral", className, children, ...props }: HTMLAttributes<HTMLSpanElement> & { tone?: StatusTone }) {
  return (
    <span className={cx("inline-flex min-h-7 items-center rounded-md px-2.5 py-1 text-xs font-semibold ring-1", statusClasses[tone], className)} {...props}>
      {children}
    </span>
  );
}

export function DashboardCard({ className, children, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cx("rounded-lg border border-slate-200 bg-white p-4 shadow-sm transition hover:border-emerald-200 hover:shadow-md", className)} {...props}>
      {children}
    </div>
  );
}

export function SectionHeader({ eyebrow, title, description, action, className }: { eyebrow?: string; title: string; description?: string; action?: ReactNode; className?: string }) {
  return (
    <div className={cx("flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between", className)}>
      <div>
        {eyebrow ? <p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">{eyebrow}</p> : null}
        <h2 className="text-xl font-semibold text-slate-950">{title}</h2>
        {description ? <p className="mt-1 max-w-2xl text-sm leading-6 text-slate-600">{description}</p> : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

export function ProgressPill({ value, max = 100, label, className }: { value: number; max?: number; label?: string; className?: string }) {
  const safeMax = max > 0 ? max : 100;
  const boundedValue = Math.min(Math.max(value, 0), safeMax);
  const percent = Math.round((boundedValue / safeMax) * 100);

  return (
    <div className={cx("inline-flex min-h-8 items-center gap-2 rounded-md bg-amber-50 px-3 py-1 text-xs font-semibold text-amber-800 ring-1 ring-amber-200", className)}>
      <span className="h-2 w-16 overflow-hidden rounded bg-amber-100">
        <span className="block h-full rounded bg-amber-400" style={{ width: `${percent}%` }} />
      </span>
      <span>{label || `${percent}%`}</span>
    </div>
  );
}
