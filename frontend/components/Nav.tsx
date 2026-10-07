"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { api } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

const LINKS = [
  { href: "/", label: "Dashboard" },
  { href: "/jobs", label: "Jobs" },
  { href: "/review", label: "Needs you" },
  { href: "/settings", label: "Settings" },
];

export function Nav() {
  const path = usePathname();
  const { data: stats } = usePoll(api.stats, 5000);
  const { data: agent } = usePoll(api.agent, 3000);
  const needsYou = (stats?.manual_action_required ?? 0) + (agent?.gate_kind ? 1 : 0);
  return (
    <nav className="nav">
      {LINKS.map((l) => {
        const active = l.href === "/" ? path === "/" : path.startsWith(l.href);
        return (
          <Link key={l.href} href={l.href} className={active ? "active" : ""}>
            <span>{l.label}</span>
            {l.href === "/review" && needsYou > 0 && <span className="badge warn">{needsYou}</span>}
          </Link>
        );
      })}
    </nav>
  );
}
