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
		<section className="rounded-lg border border-slate-200 bg-white shadow-sm">
			<button
				type="button"
				className="flex w-full items-start justify-between gap-4 px-4 py-3 text-left"
				aria-expanded={open}
				onClick={() => setOpen((current) => !current)}
			>
				<span>
					<span className="block text-sm font-semibold text-slate-950">{title}</span>
					{summary ? <span className="mt-1 block text-xs leading-5 text-slate-500">{summary}</span> : null}
				</span>
				<span aria-hidden="true" className="rounded-md bg-slate-100 px-2 py-0.5 text-base font-semibold text-slate-700">
					{open ? "-" : "+"}
				</span>
			</button>
			{open ? <div className="border-t border-slate-100 px-4 py-4 text-sm text-slate-700">{children}</div> : null}
		</section>
	);
}
