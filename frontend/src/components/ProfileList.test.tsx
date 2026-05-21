import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Profile } from "../lib/api";
import { ProfileList } from "./ProfileList";

function makeProfile(
  id: string,
  name: string,
  status: Profile["status"],
): Profile {
  return {
    id,
    name,
    fingerprint_seed: 12345,
    proxy: null,
    timezone: null,
    locale: null,
    platform: "windows",
    user_agent: null,
    screen_width: 1920,
    screen_height: 1080,
    gpu_vendor: null,
    gpu_renderer: null,
    hardware_concurrency: null,
    humanize: false,
    human_preset: "default",
    headless: false,
    geoip: false,
    clipboard_sync: true,
    auto_launch: false,
    color_scheme: null,
    launch_args: [],
    notes: null,
    user_data_dir: `/data/profiles/${id}`,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    tags: [],
    status,
    vnc_ws_port: status === "running" ? 6100 : null,
    cdp_url: status === "running" ? `/api/profiles/${id}/cdp` : null,
  };
}

describe("ProfileList", () => {
  it("sorts running profiles first, then sorts each group by name", () => {
    render(
      <ProfileList
        profiles={[
          makeProfile("stopped-delta", "delta", "stopped"),
          makeProfile("running-bravo", "bravo", "running"),
          makeProfile("stopped-charlie", "Charlie", "stopped"),
          makeProfile("running-alpha", "Alpha", "running"),
        ]}
        selectedId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
      />,
    );

    const renderedNames = screen
      .getAllByText(/^(Alpha|bravo|Charlie|delta)$/)
      .map((node) => node.textContent);

    expect(renderedNames).toEqual(["Alpha", "bravo", "Charlie", "delta"]);
  });
});
