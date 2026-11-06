import { Suspense } from "react";

import SettingsPageClient from "./SettingsPageClient";

export default function SettingsPage() {
	return (
		<Suspense fallback={<p className="dashboard-loading">Loading settings...</p>}>
			<SettingsPageClient />
		</Suspense>
	);
}
