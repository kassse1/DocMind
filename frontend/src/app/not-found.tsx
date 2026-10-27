import { Logo } from "@/components/logo";
import { LinkButton } from "@/components/ui";

export default function NotFound() {
  return (
    <main id="main" className="flex min-h-dvh flex-col items-center justify-center gap-6 px-4 text-center">
      <Logo className="text-lg" />
      <div className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">Page not found</h1>
        <p className="text-sm text-muted">The page you are looking for does not exist.</p>
      </div>
      <LinkButton href="/dashboard" variant="primary">
        Go to the dashboard
      </LinkButton>
    </main>
  );
}
