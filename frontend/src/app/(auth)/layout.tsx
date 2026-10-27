import type { ReactNode } from "react";

import { Logo } from "@/components/logo";

export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className="grid min-h-dvh lg:grid-cols-[1fr_minmax(0,34rem)]">
      <aside className="relative hidden overflow-hidden bg-surface-muted lg:flex lg:flex-col lg:justify-between lg:p-12">
        <Logo className="text-lg" />
        <div className="max-w-md space-y-6">
          <h2 className="text-3xl font-semibold leading-tight tracking-tight">
            Answers from your own documents, with the passages to check them.
          </h2>
          <ul className="space-y-3 text-sm text-muted">
            <li>Upload PDF, DOCX and TXT files; they are indexed in the background.</li>
            <li>Semantic and hybrid search across everything you uploaded.</li>
            <li>Questions answered only from your documents, citing the exact passages, or an honest “not enough evidence”.</li>
          </ul>
        </div>
        <p className="text-xs text-muted">Your documents are visible to your account only.</p>
      </aside>
      <main id="main" className="flex items-center justify-center px-4 py-12 sm:px-8">
        <div className="w-full max-w-sm">
          <Logo className="mb-10 text-lg lg:hidden" />
          {children}
        </div>
      </main>
    </div>
  );
}
