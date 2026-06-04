import { useEffect, useRef, useState } from "react";
import { ChevronDown, KeyRound, LogOut, UserRound } from "lucide-react";
import { cn } from "@/lib/utils";

interface AdminSession {
  username: string;
  password_url: string;
  logout_url: string;
}

export function AdminProxyProfile() {
  const [session, setSession] = useState<AdminSession | null>(null);
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;

    fetch("/__admin/session", {
      credentials: "include",
      headers: { Accept: "application/json" },
    })
      .then(async (response) => {
        if (!response.ok) return null;
        const contentType = response.headers.get("content-type") ?? "";
        if (!contentType.includes("application/json")) return null;
        return (await response.json()) as AdminSession;
      })
      .then((data) => {
        if (!cancelled && data?.username) setSession(data);
      })
      .catch(() => {
        if (!cancelled) setSession(null);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (rootRef.current?.contains(event.target as Node)) return;
      setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  if (!session) return null;

  return (
    <div
      ref={rootRef}
      className="relative shrink-0 border-t border-current/10 px-3 py-2"
    >
      {open && (
        <div
          className={cn(
            "absolute bottom-full left-3 right-3 z-50 mb-2 overflow-hidden",
            "border border-border bg-popover shadow-lg",
          )}
          role="menu"
          aria-label="Admin profile actions"
        >
          <a
            className="flex items-center gap-2 px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-muted/40 hover:text-foreground"
            href={session.password_url}
            role="menuitem"
          >
            <KeyRound className="h-4 w-4" />
            <span>Change password</span>
          </a>

          <form action={session.logout_url} method="post">
            <button
              className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-muted-foreground transition-colors hover:bg-muted/40 hover:text-foreground"
              role="menuitem"
              type="submit"
            >
              <LogOut className="h-4 w-4" />
              <span>Logout</span>
            </button>
          </form>
        </div>
      )}

      <button
        type="button"
        className={cn(
          "flex w-full items-center gap-3 rounded-md border border-border/70",
          "bg-background/45 px-3 py-2 text-left transition-colors",
          "hover:border-primary/35 hover:bg-muted/35",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/35",
        )}
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-haspopup="menu"
      >
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary/12 text-primary">
          <UserRound className="h-4 w-4" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-semibold text-foreground">
            {session.username}
          </span>
          <span className="block truncate text-xs text-muted-foreground">
            Hermes Admin
          </span>
        </span>
        <ChevronDown
          className={cn(
            "h-4 w-4 shrink-0 text-muted-foreground transition-transform",
            open && "rotate-180",
          )}
        />
      </button>
    </div>
  );
}
