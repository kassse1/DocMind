"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { useAuth } from "@/components/auth-provider";
import { AskIcon, CloseIcon, FileIcon, HomeIcon, MenuIcon, SearchIcon, UserIcon } from "@/components/icons";
import { Logo } from "@/components/logo";
import { Alert, Button, LoadingBlock } from "@/components/ui";
import { describeError } from "@/lib/api/errors";
import { classNames } from "@/lib/format";

const NAV = [
  { href: "/dashboard", label: "Dashboard", icon: HomeIcon },
  { href: "/documents", label: "Documents", icon: FileIcon },
  { href: "/search", label: "Search", icon: SearchIcon },
  { href: "/ask", label: "Ask", icon: AskIcon },
  { href: "/account", label: "Account", icon: UserIcon },
] as const;

function NavLinks({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  return (
    <ul className="space-y-1">
      {NAV.map(({ href, label, icon: Icon }) => {
        const active = pathname === href || pathname.startsWith(`${href}/`);
        return (
          <li key={href}>
            <Link
              href={href}
              onClick={onNavigate}
              aria-current={active ? "page" : undefined}
              className={classNames(
                "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                active ? "bg-accent-soft text-accent" : "text-muted hover:bg-surface-muted hover:text-foreground",
              )}
            >
              <Icon />
              {label}
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

function UserMenu() {
  const { user, signOut } = useAuth();
  const [pending, setPending] = useState(false);
  return (
    <div className="flex items-center gap-3">
      <span className="hidden max-w-[16rem] truncate text-sm text-muted sm:inline" title={user?.email}>
        {user?.email}
      </span>
      <Button
        variant="secondary"
        size="sm"
        pending={pending}
        onClick={async () => {
          setPending(true);
          // signOut clears the local session even if the server is unreachable.
          await signOut().catch(() => undefined);
        }}
      >
        Sign out
      </Button>
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const { status, restoreError, retryRestore } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    if (status === "anonymous") {
      router.replace(`/login?next=${encodeURIComponent(pathname)}`);
    }
  }, [status, pathname, router]);

  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && setMenuOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  if (status !== "authenticated") {
    return (
      <main id="main" className="mx-auto flex min-h-dvh max-w-md flex-col justify-center px-4">
        {restoreError ? (
          <Alert
            title="Could not restore your session"
            action={
              <Button size="sm" variant="secondary" onClick={retryRestore}>
                Try again
              </Button>
            }
          >
            {describeError(restoreError)}
          </Alert>
        ) : (
          <LoadingBlock label={status === "anonymous" ? "Redirecting to sign in" : "Loading your workspace"} />
        )}
      </main>
    );
  }

  return (
    <div className="min-h-dvh lg:grid lg:grid-cols-[15rem_minmax(0,1fr)]">
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-accent px-3 py-2 text-sm text-accent-foreground focus:not-sr-only focus:fixed focus:top-3 focus:left-3"
      >
        Skip to content
      </a>

      <aside className="hidden border-r border-border bg-surface lg:flex lg:flex-col lg:gap-8 lg:px-4 lg:py-6">
        <Link href="/dashboard" className="px-2">
          <Logo />
        </Link>
        <nav aria-label="Main">
          <NavLinks />
        </nav>
      </aside>

      {menuOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <button
            type="button"
            className="absolute inset-0 bg-black/40"
            aria-label="Close menu"
            onClick={() => setMenuOpen(false)}
          />
          <nav
            id="mobile-nav"
            aria-label="Main"
            className="absolute inset-y-0 left-0 flex w-72 max-w-[85vw] flex-col gap-6 bg-surface px-4 py-5 shadow-xl"
          >
            <div className="flex items-center justify-between px-2">
              <Logo />
              <Button variant="ghost" size="sm" aria-label="Close menu" onClick={() => setMenuOpen(false)}>
                <CloseIcon />
              </Button>
            </div>
            <NavLinks onNavigate={() => setMenuOpen(false)} />
          </nav>
        </div>
      )}

      <div className="flex min-w-0 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center justify-between gap-3 border-b border-border bg-surface/90 px-4 backdrop-blur sm:px-6">
          <div className="flex items-center gap-2 lg:invisible">
            <Button
              variant="ghost"
              size="sm"
              aria-label="Open menu"
              aria-expanded={menuOpen}
              aria-controls="mobile-nav"
              onClick={() => setMenuOpen(true)}
              className="lg:hidden"
            >
              <MenuIcon />
            </Button>
            <Link href="/dashboard" className="lg:hidden">
              <Logo />
            </Link>
          </div>
          <UserMenu />
        </header>
        <main id="main" tabIndex={-1} className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-6 lg:px-8">
          {children}
        </main>
      </div>
    </div>
  );
}
