import { Suspense } from "react";

import SettingsPageClient from "./SettingsPageClient";

export default function SettingsPage() {
	return (
		<Suspense fallback={<p className="text-sm text-slate-500">Loading settings...</p>}>
			<SettingsPageClient />
		</Suspense>
	);
}
