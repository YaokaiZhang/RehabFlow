import { Atkinson_Hyperlegible_Next, Gaegu } from "next/font/google";

import ProductShell from "@/components/ProductShell";
import "./globals.css";
import "./styles/shell.css";
import "./styles/triage.css";
import "./styles/episode.css";
import "./styles/dashboard.css";
import "./styles/auth.css";
import "./styles/doctor.css";

const bodyFont = Atkinson_Hyperlegible_Next({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700", "800"],
  display: "swap",
  variable: "--rf-font",
});

// Handwritten display face for headlines, the logo and margin notes.
const handFont = Gaegu({
  subsets: ["latin"],
  weight: ["400", "700"],
  display: "swap",
  variable: "--rf-font-hand",
});

export const metadata = {
  title: "RehabFlow",
  description: "Patient-first AI rehab triage and guided recovery",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${bodyFont.variable} ${handFont.variable}`}>
      <body>
        <ProductShell>{children}</ProductShell>
      </body>
    </html>
  );
}
