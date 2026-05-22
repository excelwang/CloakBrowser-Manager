import { describe, expect, it } from "vitest";
import type { Profile } from "./lib/api";
import { getLongestRunningProfile, shouldOpenOnlyRunningProfile } from "./App";

function makeProfile(
  id: string,
  name: string,
  status: Profile["status"],
  launchedAt: string | null,
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
    launched_at: launchedAt,
  };
}

describe("getLongestRunningProfile", () => {
  it("returns the oldest launched running profile while ignoring stopped profiles", () => {
    const result = getLongestRunningProfile([
      makeProfile("stopped", "Stopped", "stopped", null),
      makeProfile("newer", "Newer", "running", "2026-01-01T01:00:00Z"),
      makeProfile("older", "Older", "running", "2026-01-01T00:00:00Z"),
    ]);

    expect(result?.id).toBe("older");
  });

  it("can exclude the current profile when choosing a fallback VNC", () => {
    const result = getLongestRunningProfile([
      makeProfile("current", "Current", "running", "2026-01-01T00:00:00Z"),
      makeProfile("fallback", "Fallback", "running", "2026-01-01T01:00:00Z"),
    ], "current");

    expect(result?.id).toBe("fallback");
  });
});

describe("shouldOpenOnlyRunningProfile", () => {
  it("opens the only running profile on initial observation", () => {
    expect(shouldOpenOnlyRunningProfile(null, 1)).toBe(true);
  });

  it("opens a newly running profile when the previous running count was zero", () => {
    expect(shouldOpenOnlyRunningProfile(0, 1)).toBe(true);
  });

  it("does not switch when another profile was already running", () => {
    expect(shouldOpenOnlyRunningProfile(1, 2)).toBe(false);
  });
});
