"use client";

import { type FormEvent, useMemo, useState } from "react";

import { type MemoryDocumentFieldInput } from "@rehab/shared/api";

import { AppButton, DashboardCard, SectionHeader } from "../../components/ui";
import { usePatientMemoryDocument } from "../memory/usePatientMemoryDocument";

const emptyForm: MemoryDocumentFieldInput = {
	field_name: "",
	value: "",
};

function formFromItem(item: MemoryDocumentFieldInput): MemoryDocumentFieldInput {
	return {
		field_name: item.field_name,
		value: item.value,
	};
}

export default function PatientMemorySettingsSection() {
	const { auth, memoryDocument, loading, error, statusMessage, busy, addItem, editItem, deleteItem } = usePatientMemoryDocument();
	const [newItem, setNewItem] = useState<MemoryDocumentFieldInput>(emptyForm);
	const [editingItemId, setEditingItemId] = useState<string | null>(null);
	const [editingItem, setEditingItem] = useState<MemoryDocumentFieldInput>(emptyForm);

	const activeItems = useMemo(
		() =>
			Object.entries(memoryDocument?.editable_fields || {}).map(([field_name, value]) => ({
				field_name,
				value: typeof value === "string" ? value : JSON.stringify(value, null, 2) || String(value),
			})),
		[memoryDocument]
	);

	const submitNewItem = async (event: FormEvent<HTMLFormElement>) => {
		event.preventDefault();
		if (!newItem.field_name.trim() || !newItem.value.trim()) return;
		const saved = await addItem({
			field_name: newItem.field_name.trim(),
			value: newItem.value.trim(),
		});
		if (saved) setNewItem(emptyForm);
	};

	const startEditing = (item: MemoryDocumentFieldInput) => {
		setEditingItemId(item.field_name);
		setEditingItem(formFromItem(item));
	};

	const submitEditItem = async (event: FormEvent<HTMLFormElement>) => {
		event.preventDefault();
		if (!editingItemId || !editingItem.field_name.trim() || !editingItem.value.trim()) return;
		const updated = await editItem(editingItemId, {
			field_name: editingItem.field_name.trim(),
			value: editingItem.value.trim(),
		});
		if (updated) {
			setEditingItemId(null);
			setEditingItem(emptyForm);
		}
	};

	if (!auth || auth.role !== "patient") {
		return (
			<DashboardCard className="max-w-2xl">
				<SectionHeader
					eyebrow="Patient Memory"
					title="Sign in as a patient to manage Patient Memory"
					description="Patient Memory stores durable context patients can Directly edit, while Episode Memory stays system-managed for each care concern."
				/>
			</DashboardCard>
		);
	}

	return (
		<section className="space-y-5" id="patient-memory">
			<DashboardCard className="p-5 md:p-6">
				<SectionHeader
					eyebrow="Patient Memory"
					title="Patient Memory"
					description="Directly edit the durable details RehabFlow should remember across Care Episodes. Episode Memory is read-only here and system-managed from triage, rehab sessions, and Professional Care activity."
				/>
			</DashboardCard>

			{error ? <p className="rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</p> : null}
			{statusMessage ? <p className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-700">{statusMessage}</p> : null}
			{loading ? <p className="text-sm text-slate-500">Loading Patient Memory...</p> : null}

			<DashboardCard className="p-5 md:p-6">
				<div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
					<div>
						<p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Patient Memory summary</p>
						<h3 className="mt-1 text-lg font-semibold text-slate-950">Memory summary</h3>
					</div>
					<p className="text-xs font-medium text-slate-500">{memoryDocument?.last_compiled_at ? new Date(memoryDocument.last_compiled_at).toLocaleString() : "Not compiled yet"}</p>
				</div>
				<div className="mt-4 whitespace-pre-wrap rounded-md border border-slate-200 bg-slate-50 p-4 text-sm leading-6 text-slate-700">
					{memoryDocument?.compiled_text || "No Patient Memory has been compiled yet. Add a memory item below to start."}
				</div>
			</DashboardCard>

			<DashboardCard className="p-5 md:p-6">
				<h3 className="text-lg font-semibold text-slate-950">Add memory field</h3>
				<form className="mt-4 grid gap-3" onSubmit={submitNewItem}>
					<label className="grid gap-1 text-sm font-medium text-slate-700">
						<span>Field name</span>
						<input className="input" value={newItem.field_name} onChange={(event) => setNewItem((prev) => ({ ...prev, field_name: event.target.value }))} placeholder="preferred_activity" />
					</label>
					<label className="grid gap-1 text-sm font-medium text-slate-700">
						<span>Value</span>
						<textarea className="textarea min-h-28" value={newItem.value} onChange={(event) => setNewItem((prev) => ({ ...prev, value: event.target.value }))} placeholder="Write the durable context you want RehabFlow to remember." />
					</label>
					<div>
						<AppButton type="submit" disabled={busy || !newItem.field_name.trim() || !newItem.value.trim()}>{busy ? "Saving" : "Add field"}</AppButton>
					</div>
				</form>
			</DashboardCard>

			<section className="space-y-3">
				<SectionHeader eyebrow="Directly edit" title="Editable patient memory fields" />
				{!loading && activeItems.length === 0 ? (
					<p className="rounded-md border border-slate-200 bg-white p-4 text-sm text-slate-600">No editable Patient Memory fields yet.</p>
				) : null}
				{activeItems.map((item) => (
					<DashboardCard key={item.field_name} className="p-5">
						{editingItemId === item.field_name ? (
							<form className="grid gap-3" onSubmit={submitEditItem}>
								<label className="grid gap-1 text-sm font-medium text-slate-700">
									<span>Field name</span>
									<input className="input" value={editingItem.field_name} onChange={(event) => setEditingItem((prev) => ({ ...prev, field_name: event.target.value }))} />
								</label>
								<label className="grid gap-1 text-sm font-medium text-slate-700">
									<span>Value</span>
									<textarea className="textarea min-h-28" value={editingItem.value} onChange={(event) => setEditingItem((prev) => ({ ...prev, value: event.target.value }))} />
								</label>
								<div className="flex flex-wrap gap-2">
									<AppButton type="submit" disabled={busy || !editingItem.field_name.trim() || !editingItem.value.trim()}>{busy ? "Saving" : "Save"}</AppButton>
									<AppButton variant="secondary" type="button" onClick={() => setEditingItemId(null)} disabled={busy}>Cancel</AppButton>
								</div>
							</form>
						) : (
							<>
								<div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
									<div>
										<p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Patient Memory field</p>
										<h3 className="mt-1 text-lg font-semibold text-slate-950">{item.field_name}</h3>
									</div>
								</div>
								<p className="mt-3 whitespace-pre-wrap text-sm leading-6 text-slate-700">{item.value}</p>
								<div className="mt-4 flex flex-wrap gap-2">
									<AppButton variant="secondary" type="button" onClick={() => startEditing(item)} disabled={busy}>Edit</AppButton>
									<AppButton variant="secondary" type="button" onClick={() => deleteItem(item.field_name)} disabled={busy}>Delete</AppButton>
								</div>
							</>
						)}
					</DashboardCard>
				))}
			</section>
		</section>
	);
}
