export function Logo({ className }: { className?: string }) {
  return (
    <span className={`inline-flex items-center gap-2 font-semibold tracking-tight ${className ?? ""}`}>
      <svg viewBox="0 0 32 32" className="size-7" aria-hidden="true">
        <rect width="32" height="32" rx="8" className="fill-accent" />
        <path d="M10 8h7.5a6.5 6.5 0 0 1 0 13H14v3h-4z" className="fill-accent-foreground" />
        <circle cx="17.2" cy="14.5" r="2.6" className="fill-accent" />
      </svg>
      <span>DocMind</span>
    </span>
  );
}
