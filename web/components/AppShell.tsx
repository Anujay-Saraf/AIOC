"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Activity, AudioLines, Bell, ClipboardCheck, Home, Network, Radar, Settings } from "lucide-react";

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const onIncident = pathname.startsWith("/incident/");
  const wideWorkspace = pathname === "/jarvis" || onIncident;

  return (
    <div className="app-frame">
      <header className="topbar">
        <Link href="/" className="brand" aria-label="AI Operations Command home">
          <span className="brand-mark">
            <Radar size={20} />
          </span>
          <span>
            <strong>AIOC</strong>
            <small>Incident Command</small>
          </span>
        </Link>
        <div className="topbar-status">
          <span className="live-dot" />
          <span>Live Ops</span>
        </div>
        <nav className="desktop-nav" aria-label="Primary navigation">
          <Link className={pathname === "/jarvis" ? "active" : ""} aria-current={pathname === "/jarvis" ? "page" : undefined} href="/jarvis">Jarvis</Link>
          <Link className={pathname === "/admin" ? "active" : ""} aria-current={pathname === "/admin" ? "page" : undefined} href="/admin">Admin</Link>
        </nav>
      </header>

      <main className={`app-main ${wideWorkspace ? "app-main-wide" : ""}`}>{children}</main>

      <nav className="mobile-nav" aria-label="Primary">
        <Link className={pathname === "/" || pathname === "/dashboard" ? "active" : ""} aria-current={pathname === "/" || pathname === "/dashboard" ? "page" : undefined} href="/">
          <Home size={20} />
          <span>Home</span>
        </Link>
        {onIncident ? <>
          <a href="#incident-overview"><Bell size={20} /><span>Incident</span></a>
          <a href="#review"><ClipboardCheck size={20} /><span>Review</span></a>
          <a href="#trace"><Activity size={20} /><span>Trace</span></a>
        </> : <>
          <Link className={pathname === "/jarvis" ? "active" : ""} aria-current={pathname === "/jarvis" ? "page" : undefined} href="/jarvis"><AudioLines size={20} /><span>Jarvis</span></Link>
          <Link className={pathname === "/admin" ? "active" : ""} aria-current={pathname === "/admin" ? "page" : undefined} href="/admin"><Settings size={20} /><span>Admin</span></Link>
        </>}
      </nav>
    </div>
  );
}
