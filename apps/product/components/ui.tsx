import { ButtonHTMLAttributes, HTMLAttributes, ReactNode } from "react";

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

type AppButtonVariant = "primary" | "secondary" | "ghost" | "danger";

type AppButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: AppButtonVariant;
};

// Same family as the .btn-* classes in app/globals.css, so links styled as
// buttons and real buttons look identical.
const buttonClasses: Record<AppButtonVariant, string> = {
  primary: "btn-primary",
  secondary: "btn-secondary",
  ghost: "btn border-transparent bg-transparent text-ink hover:bg-[var(--rf-olive-tint)]",
  danger: "btn border-danger bg-danger text-surface hover:border-ink hover:bg-ink",
};

export function AppButton({ variant = "primary", className, type = "button", ...props }: AppButtonProps) {
  return <button type={type} className={cx(buttonClasses[variant], className)} {...props} />;
}

type StatusTone = "success" | "info" | "attention" | "risk" | "neutral";

const statusClasses: Record<StatusTone, string> = {
  success: "border-[rgba(82,97,78,0.3)] bg-[var(--rf-olive-tint)] text-action",
  info: "border-line bg-surface text-ink",
  attention: "border-[rgba(157,119,79,0.35)] bg-[#f5ead6] text-[#6c4f38]",
  risk: "border-[rgba(139,63,49,0.3)] bg-[var(--rf-danger-tint)] text-danger",
  neutral: "border-line bg-[var(--rf-surface-muted)] text-[var(--rf-ink-muted)]",
};

export function StatusBadge({ tone = "neutral", className, children, ...props }: HTMLAttributes<HTMLSpanElement> & { tone?: StatusTone }) {
  return (
    <span
      className={cx(
        "inline-flex min-h-7 w-fit max-w-full items-center gap-1.5 rounded-full border px-3 py-0.5 text-[0.8125rem] font-semibold leading-snug",
        statusClasses[tone],
        className,
      )}
      {...props}
    >
      {children}
    </span>
  );
}

export function DashboardCard({ className, children, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cx("dashboard-card rounded-card border border-line bg-surface p-6", className)} {...props}>
      {children}
    </div>
  );
}

export function SectionHeader({ eyebrow, title, description, action, className }: { eyebrow?: string; title: string; description?: string; action?: ReactNode; className?: string }) {
  return (
    <div className={cx("flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between", className)}>
      <div className="min-w-0">
        {eyebrow && eyebrow.trim().toLowerCase() !== title.trim().toLowerCase() ? (
          <p className="mb-1 text-sm font-semibold text-action">{eyebrow}</p>
        ) : null}
        <h2 className="text-xl font-bold leading-tight text-ink">{title}</h2>
        {description ? <p className="mt-1.5 max-w-[62ch] text-[0.9375rem] leading-relaxed text-[var(--rf-ink-muted)]">{description}</p> : null}
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
    <div className={cx("inline-flex min-h-8 w-fit items-center gap-2 rounded-full border border-line bg-surface px-3 py-1 text-[0.8125rem] font-semibold text-ink", className)}>
      <span className="h-1.5 w-16 overflow-hidden rounded-full bg-[var(--rf-surface-muted)]">
        <span className="block h-full rounded-full bg-action" style={{ width: `${percent}%` }} />
      </span>
      <span>{label || `${percent}%`}</span>
    </div>
  );
}
