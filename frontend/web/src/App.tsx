/** The console shell: masthead + three columns (PRD section 14 layout). */
import { useEffect, useReducer, useState } from "react";

import { fetchMe, fetchMeta, fetchUsers, streamChat } from "./api";
import { Chat } from "./components/Chat";
import { ActivityLog } from "./components/ActivityLog";
import { Masthead } from "./components/Masthead";
import { AttestationCard, ChecksCard, ContextCard } from "./components/Rail";
import { initialState, reducer } from "./state";
import type { ConsoleMeta, Persona } from "./types";

/** Config-plane poll: a rejected reload must appear without a page reload (FR-CFG-3). */
const META_POLL_MS = 5000;

/** Threads survive page reloads (the server keeps the ADK session by
 * contextId anyway; losing the rendered transcript to a refresh read as
 * "my App 360 disappeared"). sessionStorage keeps it per-tab; a failed
 * or blocked read simply starts fresh. */
const PERSIST_KEY = "cloudops.thread.v1";

function loadPersisted(): typeof initialState {
  try {
    const raw = sessionStorage.getItem(PERSIST_KEY);
    if (!raw) return initialState;
    const saved = JSON.parse(raw) as typeof initialState;
    // A reload mid-stream loses the in-flight turn; never resurrect busy.
    return { ...initialState, ...saved, busy: false, suppressText: false };
  } catch {
    return initialState;
  }
}

export default function App() {
  const [state, dispatch] = useReducer(reducer, initialState, loadPersisted);

  useEffect(() => {
    try {
      sessionStorage.setItem(PERSIST_KEY, JSON.stringify(state));
    } catch {
      // Quota or privacy mode: persistence is a convenience, not a contract.
    }
  }, [state]);
  const [users, setUsers] = useState<Persona[]>([]);
  const [selected, setSelected] = useState("");
  const [meta, setMeta] = useState<ConsoleMeta>({ env: "dev", configVersion: "" });
  const [me, setMe] = useState<Persona | null>(null);

  useEffect(() => {
    fetchUsers().then((list) => {
      setUsers(list);
      if (list.length > 0) setSelected(list[0].sub);
    });
    // oidc mode: the bearer token is the identity; show who it verified as.
    fetchMe().then((res) => setMe(res.claims ?? null));
  }, []);

  useEffect(() => {
    const poll = () => {
      fetchMeta()
        // Keep the previous object when nothing moved: a poll must not
        // re-render the transcript mid-stream.
        .then((next) => setMeta((prev) => (JSON.stringify(prev) === JSON.stringify(next) ? prev : next)))
        .catch(() => undefined);
    };
    poll();
    const timer = setInterval(poll, META_POLL_MS);
    return () => clearInterval(timer);
  }, []);

  const send = (text: string) => {
    dispatch({ type: "send", text });
    void streamChat(text, selected, state.contextId, (event) =>
      dispatch({ type: "event", event }),
    );
  };

  const selectUser = (sub: string) => {
    setSelected(sub);
    // A different identity means a different conversation: new thread.
    dispatch({ type: "reset" });
  };

  return (
    <div className="shell">
      <Masthead users={users} selected={selected} onSelect={selectUser} authMode={meta.authMode} me={me} />
      <div className="cols">
        <div className="rail-left">
          <ContextCard context={state.context} />
          <AttestationCard report={state.attestation} />
          <ChecksCard
            // Counts come from the last run when there is one, and from the
            // agent's loaded batteries before the first turn.
            attestationChecks={
              state.attestation
                ? state.attestation.clusters[0]?.checks.length ?? null
                : meta.batteries?.attestation ?? null
            }
            app360Checks={
              state.lastApp360
                ? state.lastApp360.sections.reduce((n, s) => n + s.checks.length, 0)
                : meta.batteries?.app360 ?? null
            }
            configVersion={meta.configVersion}
            reloadError={meta.reloadError ?? null}
          />
        </div>
        <Chat items={state.items} busy={state.busy} onSend={send} />
        <div className="rail-right">
          <ActivityLog logs={state.logs} contextId={state.contextId} />
        </div>
      </div>
    </div>
  );
}
