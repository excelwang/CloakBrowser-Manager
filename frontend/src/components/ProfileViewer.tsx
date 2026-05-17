import { useEffect, useRef, useState } from "react";
import { ClipboardCopy, Code2, Maximize2, Minimize2, X } from "lucide-react";
import { api, type Profile } from "../lib/api";

interface ProfileViewerProps {
  profile: Profile;
  runningProfiles: Profile[];
  profileId: string;
  cdpUrl: string | null;
  clipboardSync: boolean;
  maximized: boolean;
  onSelectRunningProfile: (id: string) => void;
  onExitMaximize: () => void;
  onDisconnect: () => void;
}

// X11 keysym for V key (Ctrl is already held in VNC by the time we intercept)
const XK_v = 0x0076;

export function ProfileViewer({
  profile,
  runningProfiles,
  profileId,
  cdpUrl,
  clipboardSync: initialClipboardSync,
  maximized,
  onSelectRunningProfile,
  onExitMaximize,
  onDisconnect,
}: ProfileViewerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const rfbRef = useRef<any>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [clipboardSync, setClipboardSync] = useState(initialClipboardSync);
  const [cdpCopied, setCdpCopied] = useState(false);
  const [profileDrawerOpen, setProfileDrawerOpen] = useState(false);

  useEffect(() => {
    let rfb: any = null;
    let cancelled = false;

    async function connect() {
      try {
        // Import noVNC dynamically
        const { default: RFB } = await import("@novnc/novnc/core/rfb.js");

        if (cancelled) return;

        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = `${protocol}//${window.location.host}/api/profiles/${profileId}/vnc`;

        rfb = new RFB(containerRef.current!, wsUrl, {
          wsProtocols: ["binary"],
        });
        rfbRef.current = rfb;

        rfb.scaleViewport = true;
        rfb.resizeSession = false;
        rfb.showDotCursor = true;

        rfb.addEventListener("connect", () => {
          if (!cancelled) setConnected(true);
        });

        rfb.addEventListener("disconnect", () => {
          if (!cancelled) {
            setConnected(false);
            onDisconnect();
          }
        });

        rfb.addEventListener("securityfailure", (e: any) => {
          setError(`Security failure: ${e.detail.reason}`);
        });
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to connect");
        }
      }
    }

    connect();

    return () => {
      cancelled = true;
      if (rfb) {
        try {
          rfb.disconnect();
        } catch (err) {
          console.debug("[vnc] disconnect cleanup failed:", err);
        }
      }
      rfbRef.current = null;
    };
  }, [profileId, onDisconnect]);

  // Host→VNC: intercept Ctrl+V/Cmd+V at keydown (capture phase)
  // Must fire BEFORE noVNC's canvas listener to prevent the race condition
  useEffect(() => {
    const container = containerRef.current;
    if (!container || !clipboardSync || !connected) return;

    const handleKeyDown = async (e: KeyboardEvent) => {
      console.log("[clipboard] keydown:", e.key, "ctrl:", e.ctrlKey, "meta:", e.metaKey, "clipboardSync:", true);

      const isPaste =
        e.key === "v" && (e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey;
      if (!isPaste) return;

      console.log("[clipboard] intercepted Ctrl+V");

      // Block noVNC from sending the keystroke before clipboard is updated
      e.stopPropagation();
      e.preventDefault();

      const rfb = rfbRef.current;
      if (!rfb) {
        console.log("[clipboard] no rfb ref, aborting");
        return;
      }

      try {
        const text = await navigator.clipboard.readText();
        console.log("[clipboard] host clipboard text:", text?.substring(0, 50), "len:", text?.length);
        if (text) {
          console.log("[clipboard] calling setClipboard API...");
          await api.setClipboard(profileId, text);
          console.log("[clipboard] setClipboard API success");
        }
      } catch (err) {
        console.warn("[clipboard] error:", err);
        setClipboardSync(false);
        return;
      }

      // Send full Ctrl+V sequence to VNC. We can't rely on Ctrl still being
      // held because the user may have released it during the async API call.
      console.log("[clipboard] sending Ctrl+V to VNC");
      rfb.sendKey(0xffe3, "ControlLeft", true);   // Ctrl press
      rfb.sendKey(XK_v, "KeyV", true);             // V press
      rfb.sendKey(XK_v, "KeyV", false);            // V release
      rfb.sendKey(0xffe3, "ControlLeft", false);   // Ctrl release
    };

    // capture: true ensures we fire before noVNC's canvas listener
    container.addEventListener("keydown", handleKeyDown, true);
    return () => container.removeEventListener("keydown", handleKeyDown, true);
  }, [profileId, clipboardSync, connected]);

  // VNC→Host: listen for noVNC "clipboard" event (fired when proxy converts
  // KasmVNC BinaryClipboard type 180 → standard ServerCutText type 3)
  useEffect(() => {
    const rfb = rfbRef.current;
    console.log("[clipboard] VNC→Host effect: rfb=", !!rfb, "sync=", clipboardSync, "connected=", connected);
    if (!rfb || !clipboardSync || !connected) return;

    const handleClipboard = (e: any) => {
      const text = e.detail?.text;
      console.log("[clipboard] VNC→Host event fired, text:", text?.substring(0, 50), "len:", text?.length);
      if (text) {
        navigator.clipboard.writeText(text).then(() => {
          console.log("[clipboard] writeText success");
        }).catch((err) => {
          console.warn("[clipboard] writeText failed:", err);
        });
      }
    };

    console.log("[clipboard] registering clipboard event listener on rfb");
    rfb.addEventListener("clipboard", handleClipboard);
    return () => {
      console.log("[clipboard] removing clipboard event listener");
      rfb.removeEventListener("clipboard", handleClipboard);
    };
  }, [clipboardSync, connected]);

  // VNC→Host polling: Chrome doesn't write to X11 clipboard under KasmVNC,
  // so type 180 events won't fire for Chrome copies. Poll via Playwright CDP.
  useEffect(() => {
    if (!clipboardSync || !connected) return;

    let cancelled = false;
    let lastText = "";

    const poll = async () => {
      if (cancelled) return;
      try {
        const { text } = await api.getClipboard(profileId);
        if (text && text !== lastText) {
          lastText = text;
          console.log("[clipboard] poll: new VNC clipboard:", text.substring(0, 50), "len:", text.length);
          await navigator.clipboard.writeText(text).catch((err) =>
            console.warn("[clipboard] poll writeText failed:", err)
          );
        }
      } catch (err) {
        console.warn("[clipboard] poll error, stopping:", err);
        cancelled = true;
        return;
      }
      if (!cancelled) {
        setTimeout(poll, 2000);
      }
    };

    // Start polling after a short delay
    const timer = setTimeout(poll, 2000);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [profileId, clipboardSync, connected]);

  const toggleFullscreen = () => {
    if (!containerRef.current) return;
    if (!document.fullscreenElement) {
      containerRef.current.requestFullscreen();
      setFullscreen(true);
    } else {
      document.exitFullscreen();
      setFullscreen(false);
    }
  };

  useEffect(() => {
    const handleFsChange = () => {
      setFullscreen(!!document.fullscreenElement);
    };
    document.addEventListener("fullscreenchange", handleFsChange);
    return () => document.removeEventListener("fullscreenchange", handleFsChange);
  }, []);

  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      window.dispatchEvent(new Event("resize"));
    });
    return () => cancelAnimationFrame(frame);
  }, [maximized]);

  useEffect(() => {
    if (!maximized) setProfileDrawerOpen(false);
  }, [maximized]);

  useEffect(() => {
    if (!maximized || !profileDrawerOpen) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      setProfileDrawerOpen(false);
    };

    window.addEventListener("keydown", handleKeyDown, true);
    return () => window.removeEventListener("keydown", handleKeyDown, true);
  }, [maximized, profileDrawerOpen]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const handleWheel = (e: WheelEvent) => {
      e.preventDefault();
    };

    container.addEventListener("wheel", handleWheel, { passive: false });
    return () => container.removeEventListener("wheel", handleWheel);
  }, []);

  const rootClassName = maximized
    ? "fixed inset-0 z-50 bg-black flex flex-col"
    : "relative h-full flex flex-col";

  const handleSelectProfile = (id: string) => {
    if (id !== profile.id) {
      onSelectRunningProfile(id);
    }
    setProfileDrawerOpen(false);
  };

  const floatingProfileButton = maximized && (
    <button
      onClick={() => setProfileDrawerOpen(true)}
      className="absolute bottom-4 left-4 z-10 flex max-w-[calc(100vw-6rem)] items-center gap-1 rounded-md border border-border bg-surface-1/35 px-2 py-1.5 text-gray-200 shadow-lg transition-colors hover:bg-surface-2/55 focus:outline-none focus:ring-2 focus:ring-accent/50"
      title={profile.name}
      aria-label={`Current profile: ${profile.name}`}
    >
      <span className="h-1.5 w-1.5 flex-shrink-0 rounded-full bg-emerald-400" />
      <span className="min-w-0 truncate text-sm font-medium">{profile.name}</span>
      <span className="hidden flex-shrink-0 text-xs capitalize text-gray-500 sm:inline">
        {profile.platform}
      </span>
      <span className="flex-shrink-0 rounded bg-surface-3/45 px-1 py-0.5 text-[10px] text-gray-400">
        {runningProfiles.length}
      </span>
    </button>
  );

  const floatingExitButton = maximized && (
    <button
      onClick={onExitMaximize}
      className="absolute bottom-4 right-4 z-40 rounded-md border border-border bg-surface-1/35 p-2 text-gray-200 shadow-lg transition-colors hover:bg-surface-2/55 focus:outline-none focus:ring-2 focus:ring-accent/50"
      title="Exit maximized VNC"
      aria-label="Exit maximized VNC"
    >
      <Minimize2 className="h-4 w-4" />
    </button>
  );

  const profileDrawer = maximized && profileDrawerOpen && (
    <>
      <button
        type="button"
        className="absolute inset-0 z-20 bg-black/35"
        onClick={() => setProfileDrawerOpen(false)}
        aria-label="Close profile switcher"
      />
      <aside className="absolute inset-y-0 left-0 z-30 flex w-72 max-w-[85vw] flex-col border-r border-border bg-surface-1/95 shadow-2xl backdrop-blur">
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div className="min-w-0">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-gray-500">
              Running Profiles
            </h2>
            <p className="mt-1 text-sm text-gray-300">{runningProfiles.length} running</p>
          </div>
          <button
            type="button"
            onClick={() => setProfileDrawerOpen(false)}
            className="rounded-md p-1 text-gray-500 transition-colors hover:bg-surface-2 hover:text-gray-300 focus:outline-none focus:ring-2 focus:ring-accent/50"
            title="Close"
            aria-label="Close profile switcher"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-2">
          {runningProfiles.map((runningProfile) => {
            const isCurrent = runningProfile.id === profile.id;

            return (
              <button
                key={runningProfile.id}
                type="button"
                onClick={() => handleSelectProfile(runningProfile.id)}
                className={`mb-1 w-full rounded-md border px-3 py-2.5 text-left transition-colors ${
                  isCurrent
                    ? "border-border-hover bg-surface-3"
                    : "border-transparent hover:bg-surface-2"
                }`}
              >
                <div className="flex min-w-0 items-center gap-2">
                  <span className="h-2 w-2 flex-shrink-0 rounded-full bg-emerald-400" />
                  <span className="min-w-0 flex-1 truncate text-sm font-medium text-gray-100">
                    {runningProfile.name}
                  </span>
                </div>
                <div className="mt-1 flex items-center gap-2 pl-4">
                  <span className="text-xs capitalize text-gray-500">
                    {runningProfile.platform}
                  </span>
                  {runningProfile.proxy && (
                    <>
                      <span className="text-xs text-gray-600">·</span>
                      <span className="text-xs text-gray-500">Proxy</span>
                    </>
                  )}
                </div>
                {runningProfile.tags.length > 0 && (
                  <div className="mt-1.5 flex flex-wrap gap-1 pl-4">
                    {runningProfile.tags.map((tag) => (
                      <span
                        key={tag.tag}
                        className="rounded-full bg-surface-4 px-1.5 py-0.5 text-[10px] text-gray-400"
                        style={tag.color ? { backgroundColor: `${tag.color}20`, color: tag.color } : undefined}
                      >
                        {tag.tag}
                      </span>
                    ))}
                  </div>
                )}
              </button>
            );
          })}
        </div>
      </aside>
    </>
  );

  if (error) {
    return (
      <div className={rootClassName}>
        {floatingProfileButton}
        {floatingExitButton}
        {profileDrawer}
        <div className="flex flex-1 items-center justify-center">
          <div className="text-center">
            <p className="text-red-400 text-sm mb-2">Connection failed</p>
            <p className="text-gray-500 text-xs">{error}</p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={rootClassName}>
      {floatingProfileButton}
      {floatingExitButton}
      {profileDrawer}

      {/* Toolbar */}
      {!maximized && (
        <div className="flex items-center justify-between px-3 py-1.5 bg-surface-1 border-b border-border">
          <div className="flex items-center gap-2">
            <span className={`h-2 w-2 rounded-full ${connected ? "bg-emerald-400" : "bg-yellow-400 animate-pulse"}`} />
            <span className="text-xs text-gray-400">
              {connected ? "Connected" : "Connecting..."}
            </span>
          </div>
          <div className="flex items-center gap-1">
            {cdpUrl && (
              <button
                onClick={() => {
                  const base = `${window.location.protocol}//${window.location.host}${cdpUrl}`;
                  navigator.clipboard?.writeText(base).then(() => {
                    setCdpCopied(true);
                    setTimeout(() => setCdpCopied(false), 2000);
                  }).catch((err) => console.warn("[cdp] copy failed:", err));
                }}
                className={`p-1 ${cdpCopied ? "text-emerald-400" : "text-gray-500 hover:text-gray-300"}`}
                title={cdpCopied ? "Copied!" : "Copy CDP endpoint URL"}
              >
                <Code2 className="h-3.5 w-3.5" />
              </button>
            )}
            <button
              onClick={() => { console.log("[clipboard] toggle:", !clipboardSync); setClipboardSync(!clipboardSync); }}
              className={`p-1 ${clipboardSync ? "text-accent" : "text-gray-500 hover:text-gray-300"}`}
              title={clipboardSync ? "Disable clipboard sync" : "Enable clipboard sync"}
              disabled={!connected}
            >
              <ClipboardCopy className="h-3.5 w-3.5" />
            </button>
            <button
              onClick={toggleFullscreen}
              className="text-gray-500 hover:text-gray-300 p-1"
              title={fullscreen ? "Exit fullscreen" : "Fullscreen"}
            >
              {fullscreen ? <Minimize2 className="h-3.5 w-3.5" /> : <Maximize2 className="h-3.5 w-3.5" />}
            </button>
          </div>
        </div>
      )}

      {/* VNC canvas container */}
      <div
        ref={containerRef}
        className="flex-1 bg-black overflow-hidden"
        style={{ minHeight: 0 }}
      />
    </div>
  );
}
