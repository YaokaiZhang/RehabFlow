import { Atkinson_Hyperlegible_Next } from "next/font/google";

import ProductShell from "@/components/ProductShell";
import "./globals.css";

const bodyFont = Atkinson_Hyperlegible_Next({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700", "800"],
  display: "swap",
  variable: "--rf-font",
});

export const metadata = {
  title: "RehabFlow",
  description: "Patient-first AI rehab triage and guided recovery",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={bodyFont.variable}>
      <body>
        <ProductShell>{children}</ProductShell>
      </body>
    </html>
  );
}
