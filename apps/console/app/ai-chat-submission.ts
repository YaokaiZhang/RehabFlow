export type AiChatSubmission = {
  message: string;
  sessionId: string;
  idempotencyKey: string;
};

export function resolveAiChatSubmission(
  pending: AiChatSubmission | null,
  message: string,
  sessionId: string,
  createIdempotencyKey: () => string,
): { submission: AiChatSubmission; isRetry: boolean } {
  const normalizedMessage = message.trim();
  const normalizedSessionId = sessionId.trim() || "new";

  if (
    pending &&
    pending.message === normalizedMessage &&
    pending.sessionId === normalizedSessionId
  ) {
    return { submission: pending, isRetry: true };
  }

  return {
    submission: {
      message: normalizedMessage,
      sessionId: normalizedSessionId,
      idempotencyKey: createIdempotencyKey(),
    },
    isRetry: false,
  };
}
