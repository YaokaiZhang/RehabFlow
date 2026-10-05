import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const productRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const pagePath = resolve(productRoot, "app/page.tsx");

function assert(condition, message) {
	if (!condition) throw new Error(message);
}

assert(existsSync(pagePath), "public triage page should exist");

const page = readFileSync(pagePath, "utf8");

for (const vocabulary of ["Professional Care"]) {
	assert(page.includes(vocabulary), `public triage should use current ${vocabulary} vocabulary`);
}

for (const removedCopy of ["The assistant will ask clarifying questions", "Optional Live Movement Monitoring can support"]) {
	assert(!page.includes(removedCopy), `public triage should remove ${removedCopy}`);
}

for (const legacyCopy of ["professional monitoring", "monitoring path"]) {
	assert(!page.toLowerCase().includes(legacyCopy), `public triage should not use legacy ${legacyCopy} copy`);
}

assert(!page.includes("Patient Dashboard"), "public triage should not render a Patient Dashboard action");
assert(
	page.includes('/login') && page.includes('/register'),
	'anonymous patients should retain Login and Register routes',
);
assert(page.includes('href="/rehab/session"'), "public triage should retain the demo rehab session link");
assert(page.includes("getCareEpisode"), "episode-scoped triage should load the selected Care Episode");
assert(page.includes("episodeTitleLoading"), "episode-scoped triage should represent episode-title loading");
assert(page.includes("Loading rehab episode..."), "episode-scoped triage should explain title loading");
assert(
	page.includes('episodeTitle || "What does your body need today?"'),
	"new triage without an episode should retain the generic prompt",
);

console.log("public triage copy contract ok");
