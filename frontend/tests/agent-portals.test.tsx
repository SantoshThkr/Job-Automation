import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { AgentControls } from "@/components/AgentControls";
import { api, type Portal } from "@/lib/api";
import { idleAgent } from "./helpers";

afterEach(cleanup);

it("starts with only the supported portals from a pre-revert agent selection", () => {
  const supported: Portal[] = ["naukri", "linkedin", "instahyre"];
  const start = vi.spyOn(api, "start").mockResolvedValue(idleAgent);
  const agent = { ...idleAgent, portals: ["indeed", ...supported] as Portal[] };
  render(<AgentControls agent={agent} defaultPortals={supported} onChange={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Start" }));
  expect(start).toHaveBeenCalledWith("dry_run", supported);
});

it("requires a visible selection when the only saved portal was retired", () => {
  const agent = { ...idleAgent, portals: ["indeed"] as unknown as Portal[] };
  render(<AgentControls agent={agent} defaultPortals={["naukri"]} onChange={() => {}} />);
  expect((screen.getByRole("button", { name: "Start" }) as HTMLButtonElement).disabled).toBe(true);
});
