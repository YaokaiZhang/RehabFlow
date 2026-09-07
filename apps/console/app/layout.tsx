import "./globals.css";

export const metadata = {
  title: "Rehab Internal QA Console",
  description: "Internal diagnostics and QA console for the rehabilitation platform",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="border-b border-slate-200 bg-white">
          <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-3">
            <div>
              <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Internal QA</p>
              <h1 className="text-lg font-semibold text-slate-950">Rehab Console</h1>
            </div>
            <span className="rounded bg-amber-100 px-3 py-1 text-xs font-semibold text-amber-800">
              Separate QA app
            </span>
          </div>
        </header>
        <main className="mx-auto max-w-7xl px-4 py-6">{children}</main>
      </body>
    </html>
  );
}
