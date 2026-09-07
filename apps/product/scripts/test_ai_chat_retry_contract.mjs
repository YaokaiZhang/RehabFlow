import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const productRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const page = readFileSync(resolve(productRoot, "app/page.tsx"), "utf8");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const sendTurnStart = page.indexOf("const sendChatTurn = async");
const errorBranchStart = page.indexOf('if (data.event === "error")', sendTurnStart);
const clearPending = page.indexOf("pendingSubmissionRef.current = null;", sendTurnStart);
assert(sendTurnStart >= 0, "AI chat page should define sendChatTurn");
assert(errorBranchStart >= 0, "AI chat page should handle application-level AI errors");
assert(clearPending > errorBranchStart, "pending submission should be cleared only after the application-error branch");
const errorBranchEnd = page.indexOf("} else {", errorBranchStart);
const errorBranch = page.slice(errorBranchStart, errorBranchEnd);
assert(errorBranch.includes("pendingSubmissionRef.current = submission"), "application-level errors should retain the pending submission");
assert(errorBranch.includes("setPrompt(submission.message)"), "application-level errors should restore the failed prompt");
assert(
  page.includes("previousSubmission?.message === text && previousSubmission.careEpisodeId === episodeId"),
  "changed messages or Care Episodes should create a new idempotency key",
);

console.log("AI chat retry contract ok");
