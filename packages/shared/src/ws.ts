import { getWsBase } from "./runtime";

export type PatientScorePayload = {
  event: string;
  patient_id: string;
  timestamp: string;
  coordinates: Array<{ x: number; y: number; z: number; visibility?: number }>;
  current_score: number;
};

function requireTicket(ticket: string): string {
  if (!ticket.trim()) throw new Error("A stream ticket is required.");
  return ticket;
}

export function connectPatientStream(ticket: string): WebSocket {
  return new WebSocket(`${getWsBase()}/rehab/stream?ticket=${encodeURIComponent(requireTicket(ticket))}`);
}

export function connectDoctorEpisodeMonitor(careEpisodeId: string, ticket: string): WebSocket {
  if (!careEpisodeId.trim()) throw new Error("Care Episode is required.");
  return new WebSocket(
    `${getWsBase()}/doctor/monitor/episodes/${encodeURIComponent(careEpisodeId)}?ticket=${encodeURIComponent(requireTicket(ticket))}`,
  );
}
