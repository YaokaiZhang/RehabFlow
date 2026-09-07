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

for (const vocabulary of ["Professional Care", "Optional Live Movement Monitoring"]) {
	assert(page.includes(vocabulary), `public triage should use current ${vocabulary} vocabulary`);
}

for (const legacyCopy of ["professional monitoring", "monitoring path"]) {
	assert(!page.toLowerCase().includes(legacyCopy), `public triage should not use legacy ${legacyCopy} copy`);
}

assert(
	/patientId\s*\?\s*\([\s\S]*?href="\/episodes"[\s\S]*?>\s*Patient Dashboard\s*</.test(page),
	"signed-in patients should have a Patient Dashboard continuation",
);
assert(
	/:\s*\([\s\S]*?href="\/login"[\s\S]*?>\s*Login\s*<[\s\S]*?href="\/register"[\s\S]*?>\s*Register\s*</.test(page),
	"anonymous patients should retain Login and Register routes",
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
