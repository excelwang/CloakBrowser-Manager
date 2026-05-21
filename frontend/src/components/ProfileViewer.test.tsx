import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Profile } from "../lib/api";
import { ProfileViewer } from "./ProfileViewer";

vi.mock("@novnc/novnc/core/rfb.js", () => ({
  default: class MockRFB {
    scaleViewport = false;
    resizeSession = false;
    showDotCursor = false;
    addEventListener = vi.fn();
    removeEventListener = vi.fn();
    disconnect = vi.fn();
    sendKey = vi.fn();
  },
}));

vi.mock("../lib/api", () => ({
  api: {
    getClipboard: vi.fn(),
    setClipboard: vi.fn(),
  },
}));

const profile: Profile = {
  id: "profile-1",
  name: "Profile One",
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
  user_data_dir: "/data/profiles/profile-1",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  tags: [],
  status: "running",
  vnc_ws_port: 6100,
  cdp_url: "/api/profiles/profile-1/cdp",
};

function renderViewer(overrides: Partial<Parameters<typeof ProfileViewer>[0]> = {}) {
  const props: Parameters<typeof ProfileViewer>[0] = {
    profile,
    runningProfiles: [profile],
    profileId: profile.id,
    cdpUrl: profile.cdp_url,
    clipboardSync: profile.clipboard_sync,
    maximized: false,
    onSelectRunningProfile: vi.fn(),
    onEnterMaximize: vi.fn(),
    onExitMaximize: vi.fn(),
    onDisconnect: vi.fn(),
    ...overrides,
  };

  render(<ProfileViewer {...props} />);
  return props;
}

describe("ProfileViewer", () => {
  it("places the VNC maximize button directly after browser fullscreen", () => {
    const props = renderViewer();

    const fullscreenButton = screen.getByLabelText("Enter browser fullscreen");
    const maximizeButton = screen.getByLabelText("Maximize VNC");
    const toolbarButtons = Array.from(
      fullscreenButton.parentElement?.querySelectorAll("button") ?? [],
    );

    expect(toolbarButtons.indexOf(maximizeButton)).toBe(
      toolbarButtons.indexOf(fullscreenButton) + 1,
    );

    fireEvent.click(maximizeButton);
    expect(props.onEnterMaximize).toHaveBeenCalledOnce();
  });

  it("does not show the enter-maximize toolbar button while maximized", () => {
    renderViewer({ maximized: true });

    expect(screen.queryByLabelText("Maximize VNC")).toBeNull();
    expect(screen.getByLabelText("Exit maximized VNC")).not.toBeNull();
  });
});
