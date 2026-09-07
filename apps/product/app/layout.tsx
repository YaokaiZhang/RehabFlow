import ProductShell from "@/components/ProductShell";
import "./globals.css";

export const metadata = {
  title: "RehabFlow",
  description: "Patient-first AI rehab triage and guided recovery",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <ProductShell>{children}</ProductShell>
      </body>
    </html>
  );
}
