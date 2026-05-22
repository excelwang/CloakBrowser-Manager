import { useState, useCallback, useEffect, useMemo, useRef } from "react";
import { Lock, PanelLeftClose, PanelLeft } from "lucide-react";
import { useProfiles } from "./hooks/useProfiles";
import { api, setOnUnauthorized, type Profile, type ProfileCreateData } from "./lib/api";
import { ProfileList } from "./components/ProfileList";
import { ProfileForm } from "./components/ProfileForm";
import { ProfileViewer } from "./components/ProfileViewer";
import { LaunchButton } from "./components/LaunchButton";
import { StatusIndicator } from "./components/StatusIndicator";
import { LoginPage } from "./components/LoginPage";

type AuthState = "checking" | "required" | "ok" | "error";
type View = "empty" | "create" | "edit" | "view";

function launchTime(profile: Profile) {
  const timestamp = Date.parse(profile.launched_at ?? "");
  return Number.isNaN(timestamp) ? Number.POSITIVE_INFINITY : timestamp;
}

export function getLongestRunningProfile(
  profiles: Profile[],
  excludeId?: string | null,
) {
  return [...profiles]
    .filter((profile) => profile.status === "running" && profile.id !== excludeId)
    .sort((a, b) => {
      const timeOrder = launchTime(a) - launchTime(b);
      if (timeOrder !== 0) return timeOrder;

      const nameOrder = a.name.localeCompare(b.name, undefined, {
        sensitivity: "base",
      });
      if (nameOrder !== 0) return nameOrder;

      return a.id.localeCompare(b.id);
    })[0] ?? null;
}

export default function App() {
  const [authState, setAuthState] = useState<AuthState>("checking");
  const [authRequired, setAuthRequired] = useState(false);

  useEffect(() => {
    setOnUnauthorized(() => setAuthState("required"));

    api.authStatus()
      .then(({ auth_required, authenticated }) => {
        setAuthRequired(auth_required);
        if (!auth_required || authenticated) {
          setAuthState("ok");
        } else {
          setAuthState("required");
        }
      })
      .catch((err) => {
        console.warn("[auth] status check failed:", err);
        setAuthState("error");
      });

    return () => setOnUnauthorized(null);
  }, []);

  if (authState === "checking") {
    return (
      <div className="h-screen flex items-center justify-center">
        <div className="text-gray-500 text-sm">Loading...</div>
      </div>
    );
  }

  if (authState === "error") {
    return (
      <div className="h-screen flex items-center justify-center bg-surface-0">
        <div className="text-center">
          <p className="text-red-400 text-sm mb-2">Unable to reach the server</p>
          <button
            onClick={() => {
              setAuthState("checking");
              api.authStatus()
                .then(({ auth_required, authenticated }) => {
                  setAuthRequired(auth_required);
                  setAuthState(!auth_required || authenticated ? "ok" : "required");
                })
                .catch(() => setAuthState("error"));
            }}
            className="text-xs text-gray-400 hover:text-gray-200 underline"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (authState === "required") {
    return <LoginPage onSuccess={() => setAuthState("ok")} />;
  }

  return (
    <AppContent
      authRequired={authRequired}
      onLogout={async () => {
        await api.logout();
        setAuthState("required");
      }}
    />
  );
}

interface AppContentProps {
  authRequired: boolean;
  onLogout: () => void;
}

function AppContent({ authRequired, onLogout }: AppContentProps) {
  const { profiles, loading, error, create, update, remove, launch, stop } = useProfiles();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [view, setView] = useState<View>("empty");
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [vncMaximized, setVncMaximized] = useState(false);
  const previousRunningCountRef = useRef<number | null>(null);

  const selected = profiles.find((p) => p.id === selectedId) ?? null;
  const runningProfiles = useMemo(
    () => profiles.filter((p) => p.status === "running"),
    [profiles],
  );

  useEffect(() => {
    if (view !== "view") return;
    if (selected?.status === "running") return;

    const fallbackProfile = getLongestRunningProfile(profiles, selectedId);
    if (fallbackProfile) {
      setSelectedId(fallbackProfile.id);
      setView("view");
    } else {
      setView(selected ? "edit" : "empty");
    }
    setVncMaximized(false);
  }, [profiles, selected?.id, selected?.status, selectedId, view]);

  useEffect(() => {
    if (loading) return;

    const previousRunningCount = previousRunningCountRef.current;
    previousRunningCountRef.current = runningProfiles.length;

    if (previousRunningCount !== 0 || runningProfiles.length !== 1) return;

    const onlyRunningProfile = runningProfiles[0];
    if (!onlyRunningProfile) return;

    setSelectedId(onlyRunningProfile.id);
    setView("view");
    setVncMaximized(false);
  }, [loading, runningProfiles]);

  const handleSelect = useCallback((id: string) => {
    setSelectedId(id);
    const profile = profiles.find((p) => p.id === id);
    const nextView = profile?.status === "running" ? "view" : "edit";
    setView(nextView);
    setVncMaximized(false);
  }, [profiles]);

  const handleSelectRunningProfile = useCallback((id: string) => {
    const profile = profiles.find((p) => p.id === id);
    if (profile?.status !== "running") return;

    setSelectedId(id);
    setView("view");
  }, [profiles]);

  const handleNew = useCallback(() => {
    setSelectedId(null);
    setView("create");
    setVncMaximized(false);
  }, []);

  const handleCreate = useCallback(async (data: ProfileCreateData) => {
    const profile = await create(data);
    if (profile) {
      setSelectedId(profile.id);
      setView("edit");
      setVncMaximized(false);
    }
  }, [create]);

  const handleUpdate = useCallback(async (data: ProfileCreateData) => {
    if (!selectedId) return;
    await update(selectedId, data);
  }, [selectedId, update]);

  const handleDelete = useCallback(async () => {
    if (!selectedId) return;
    await remove(selectedId);
    setSelectedId(null);
    setView("empty");
    setVncMaximized(false);
  }, [selectedId, remove]);

  const handleLaunch = useCallback(async () => {
    if (!selectedId) return;
    const result = await launch(selectedId);
    if (result) {
      setView("view");
      setVncMaximized(false);
    }
  }, [selectedId, launch]);

  const handleStop = useCallback(async () => {
    if (!selectedId) return;
    const fallbackProfile = getLongestRunningProfile(profiles, selectedId);
    await stop(selectedId);
    if (fallbackProfile) {
      setSelectedId(fallbackProfile.id);
      setView("view");
    } else {
      setView("edit");
    }
    setVncMaximized(false);
  }, [profiles, selectedId, stop]);

  const handleVncDisconnect = useCallback(() => {
    const fallbackProfile = getLongestRunningProfile(profiles, selectedId);
    if (fallbackProfile) {
      setSelectedId(fallbackProfile.id);
      setView("view");
    } else {
      setView("edit");
    }
    setVncMaximized(false);
  }, [profiles, selectedId]);

  if (loading) {
    return (
      <div className="h-screen flex items-center justify-center">
        <div className="text-gray-500 text-sm">Loading...</div>
      </div>
    );
  }

  return (
    <div className="h-screen flex">
      {/* Sidebar */}
      {sidebarOpen && (
        <div className="w-64 border-r border-border bg-surface-1 flex-shrink-0">
          <ProfileList
            profiles={profiles}
            selectedId={selectedId}
            onSelect={handleSelect}
            onNew={handleNew}
          />
        </div>
      )}

      {/* Main panel */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Top bar */}
        <div className="flex items-center justify-between px-4 py-2 border-b border-border bg-surface-1">
          <div className="flex items-center gap-3">
            <button
              onClick={() => setSidebarOpen(!sidebarOpen)}
              className="text-gray-500 hover:text-gray-300 p-1"
              title={sidebarOpen ? "Hide sidebar" : "Show sidebar"}
            >
              {sidebarOpen ? <PanelLeftClose className="h-4 w-4" /> : <PanelLeft className="h-4 w-4" />}
            </button>
            {selected && (
              <div className="flex items-center gap-2">
                <StatusIndicator status={selected.status} size="md" />
                <span className="text-sm font-medium">{selected.name}</span>
                <span className="text-xs text-gray-500 capitalize">{selected.platform}</span>
              </div>
            )}
          </div>
          <div className="flex items-center gap-2">
            {selected && (
              <LaunchButton
                status={selected.status}
                onLaunch={handleLaunch}
                onStop={handleStop}
              />
            )}
            {authRequired && (
              <button
                onClick={onLogout}
                className="text-gray-500 hover:text-gray-300 p-1"
                title="Log out"
              >
                <Lock className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        </div>

        {/* Error banner */}
        {error && (
          <div className="px-4 py-2 bg-red-600/15 border-b border-red-600/30 text-red-400 text-sm">
            {error}
          </div>
        )}

        {/* Content */}
        <div className="flex-1 overflow-y-auto overscroll-contain">
          {view === "empty" && (
            <div className="flex items-center justify-center h-full">
              <div className="text-center">
                <p className="text-gray-500 text-sm">Select a profile or create a new one</p>
              </div>
            </div>
          )}

          {view === "create" && (
            <ProfileForm
              profile={null}
              onSave={handleCreate}
              onCancel={() => {
                setView("empty");
                setVncMaximized(false);
              }}
            />
          )}

          {view === "edit" && selected && (
            <ProfileForm
              profile={selected}
              onSave={handleUpdate}
              onDelete={handleDelete}
              onCancel={() => {
                setSelectedId(null);
                setView("empty");
                setVncMaximized(false);
              }}
            />
          )}

          {view === "view" && selected && selected.status === "running" && (
            <ProfileViewer
              key={selected.id}
              profile={selected}
              runningProfiles={runningProfiles}
              profileId={selected.id}
              cdpUrl={selected.cdp_url}
              clipboardSync={selected.clipboard_sync}
              maximized={vncMaximized}
              onSelectRunningProfile={handleSelectRunningProfile}
              onEnterMaximize={() => setVncMaximized(true)}
              onExitMaximize={() => setVncMaximized(false)}
              onDisconnect={handleVncDisconnect}
            />
          )}
        </div>
      </div>
    </div>
  );
}
