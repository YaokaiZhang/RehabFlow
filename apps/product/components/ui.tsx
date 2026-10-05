import { ButtonHTMLAttributes, HTMLAttributes, ReactNode } from "react";

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

type AppButtonVariant = "primary" | "secondary" | "ghost" | "danger";

type AppButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: AppButtonVariant;
};

const buttonClasses: Record<AppButtonVariant, string> = {
  primary: "border-[var(--rf-olive-deep)] bg-[var(--rf-olive-deep)] text-[var(--rf-surface)] hover:border-[var(--rf-coffee)] hover:bg-[var(--rf-coffee)]",
  secondary: "border-[rgba(82,97,78,0.36)] bg-[rgba(201,183,156,0.34)] text-[var(--rf-coffee)] hover:border-[rgba(82,97,78,0.58)] hover:bg-[rgba(113,129,109,0.16)]",
  ghost: "border-transparent bg-transparent text-[var(--rf-coffee)] hover:bg-[rgba(113,129,109,0.12)]",
  danger: "border-[#8b3f31] bg-[#8b3f31] text-[var(--rf-surface)] hover:border-[var(--rf-coffee)] hover:bg-[var(--rf-coffee)]",
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
  success: "bg-[rgba(113,129,109,0.14)] text-[var(--rf-olive-deep)] ring-[rgba(113,129,109,0.3)]",
  info: "bg-[rgba(113,129,109,0.14)] text-[var(--rf-olive-deep)] ring-[rgba(113,129,109,0.3)]",
  attention: "bg-[rgba(201,183,156,0.34)] text-[var(--rf-coffee)] ring-[rgba(82,97,78,0.26)]",
  risk: "bg-[rgba(160,74,54,0.1)] text-[#7d392e] ring-[rgba(160,74,54,0.25)]",
  neutral: "bg-[rgba(201,183,156,0.26)] text-[var(--rf-coffee)] ring-[rgba(52,42,33,0.18)]",
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
    <div className={cx("dashboard-card rounded-[10px] border border-[var(--rf-line)] bg-[var(--rf-surface)] p-4 shadow-[0_8px_20px_rgba(52,42,33,0.06)] transition hover:border-[rgba(113,129,109,0.42)] hover:shadow-[0_10px_24px_rgba(52,42,33,0.1)]", className)} {...props}>
      {children}
    </div>
  );
}

export function SectionHeader({ eyebrow, title, description, action, className }: { eyebrow?: string; title: string; description?: string; action?: ReactNode; className?: string }) {
  return (
    <div className={cx("flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between", className)}>
      <div>
        {eyebrow ? <p className="text-xs font-semibold uppercase tracking-wide text-[var(--rf-olive-deep)]">{eyebrow}</p> : null}
        <h2 className="text-xl font-semibold text-[var(--rf-coffee)]">{title}</h2>
        {description ? <p className="mt-1 max-w-2xl text-sm leading-6 text-[rgba(52,42,33,0.7)]">{description}</p> : null}
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
