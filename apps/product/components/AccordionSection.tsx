"use client";

import { useState, type ReactNode } from "react";

type Props = {
	title: string;
	summary?: ReactNode;
	defaultOpen?: boolean;
	children: ReactNode;
};

export function AccordionSection({ title, summary, defaultOpen = false, children }: Props) {
	const [open, setOpen] = useState(defaultOpen);

	return (
		<section className="rounded-card bg-fill">
			<button
				type="button"
				className="flex w-full items-start justify-between gap-4 rounded-card px-5 py-4 text-left hover:bg-[var(--rf-fill-strong)]"
				aria-expanded={open}
				onClick={() => setOpen((current) => !current)}
			>
				<span>
					<span className="block text-base font-bold text-slate-950">{title}</span>
					{summary ? <span className="mt-0.5 line-clamp-2 block text-sm leading-snug text-slate-600">{summary}</span> : null}
				</span>
				<svg aria-hidden="true" width="18" height="18" viewBox="0 0 16 16" className={`mt-1 shrink-0 text-action transition-transform ${open ? "rotate-180" : ""}`}>
					<path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
				</svg>
			</button>
			{open ? <div className="px-5 pb-5 text-sm text-slate-700">{children}</div> : null}
		</section>
	);
}
