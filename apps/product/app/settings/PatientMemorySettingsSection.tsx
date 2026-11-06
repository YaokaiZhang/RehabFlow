"use client";

import { type FormEvent, useMemo, useState } from "react";

import { type MemoryDocumentFieldInput } from "@rehab/shared/api";

import { AppButton } from "../../components/ui";
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
			<div className="card settings-card">
				<p className="settings-empty">Sign in as a patient to manage Patient Memory.</p>
			</div>
		);
	}

	return (
		<section className="settings-content" id="patient-memory" aria-labelledby="patient-memory-title">
			{error ? <p className="notice notice-danger">{error}</p> : null}
			{statusMessage ? <p className="notice" role="status">{statusMessage}</p> : null}
			{loading ? <p className="dashboard-loading">Loading Patient Memory...</p> : null}

			<div className="sticker tone-lilac settings-memory-summary">
				<div className="settings-card-title-row">
					<h2 className="settings-card-title" id="patient-memory-title">Patient memory</h2>
					{memoryDocument?.compiled_text && memoryDocument?.last_compiled_at ? <p className="settings-meta">Updated {new Date(memoryDocument.last_compiled_at).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}</p> : null}
				</div>
				{memoryDocument?.compiled_text ? (
					<p className="settings-memory-text">{memoryDocument.compiled_text}</p>
				) : (
					<p className="settings-empty">Nothing remembered yet. Add a fact below, or keep chatting and RehabFlow will add what matters.</p>
				)}
			</div>

			<section aria-labelledby="patient-memory-items-title">
				<h3 className="settings-section-label" id="patient-memory-items-title">What RehabFlow remembers</h3>
				{!loading && activeItems.length === 0 ? <p className="settings-empty">Nothing here yet.</p> : null}
				{activeItems.length ? (
					<ul className="settings-memory-list">
						{activeItems.map((item) => (
							<li key={item.field_name} className={editingItemId === item.field_name ? "card settings-memory-item settings-memory-item-editing" : "card settings-memory-item"} data-tone="lilac">
								{editingItemId === item.field_name ? (
									<form className="settings-form" onSubmit={submitEditItem}>
										<label className="field-label" htmlFor="memory-edit-topic">Topic</label>
										<input id="memory-edit-topic" className="input" value={editingItem.field_name} onChange={(event) => setEditingItem((prev) => ({ ...prev, field_name: event.target.value }))} />
										<label className="field-label" htmlFor="memory-edit-value">Value</label>
										<textarea id="memory-edit-value" className="textarea mt-1.5 min-h-28" value={editingItem.value} onChange={(event) => setEditingItem((prev) => ({ ...prev, value: event.target.value }))} />
										<div className="settings-form-actions">
											<AppButton type="submit" disabled={busy || !editingItem.field_name.trim() || !editingItem.value.trim()}>{busy ? "Saving" : "Save"}</AppButton>
											<AppButton variant="secondary" type="button" onClick={() => setEditingItemId(null)} disabled={busy}>Cancel</AppButton>
										</div>
									</form>
								) : (
									<>
										<h4><span className="session-dot" aria-hidden="true" />{item.field_name}</h4>
										<p>{item.value}</p>
										<div className="settings-item-actions">
											<button type="button" className="settings-text-button" onClick={() => startEditing(item)} disabled={busy}>Edit<span className="sr-only"> {item.field_name}</span></button>
											<button type="button" className="settings-text-button settings-text-button-danger" onClick={() => deleteItem(item.field_name)} disabled={busy}>Delete<span className="sr-only"> {item.field_name}</span></button>
										</div>
									</>
								)}
							</li>
						))}
					</ul>
				) : null}
			</section>

			<section className="card settings-card" aria-labelledby="patient-memory-add-title">
				<h3 className="settings-card-title" id="patient-memory-add-title">Add something to remember</h3>
				<form className="settings-form" onSubmit={submitNewItem}>
					<label className="field-label" htmlFor="memory-new-topic">Topic</label>
					<input id="memory-new-topic" className="input" value={newItem.field_name} onChange={(event) => setNewItem((prev) => ({ ...prev, field_name: event.target.value }))} placeholder="For example: Favourite activity" />
					<label className="field-label" htmlFor="memory-new-value">Value</label>
					<textarea id="memory-new-value" className="textarea mt-1.5 min-h-28" value={newItem.value} onChange={(event) => setNewItem((prev) => ({ ...prev, value: event.target.value }))} placeholder="For example: I play padel twice a week and want to keep doing it." />
					<div className="settings-form-actions">
						<AppButton type="submit" variant={editingItemId ? "secondary" : "primary"} disabled={busy || !newItem.field_name.trim() || !newItem.value.trim()}>{busy ? "Saving" : "Add"}</AppButton>
					</div>
				</form>
			</section>
		</section>
	);
}
