import { redirect } from "next/navigation";

export default function PatientMemoryRedirectPage() {
	redirect("/settings?section=patient-memory");
}
