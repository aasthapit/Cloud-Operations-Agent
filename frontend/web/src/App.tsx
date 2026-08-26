/** The console shell: masthead + three columns (PRD section 14 layout). */
import { useEffect, useReducer, useState } from "react";

import { fetchMeta, fetchUsers, streamChat } from "./api";
import { Chat } from "./components/Chat";
import { ActivityLog } from "./components/ActivityLog";
import { Masthead } from "./components/Masthead";
import { AttestationCard, ChecksCard, ContextCard } from "./components/Rail";
import { initialState, reducer } from "./state";
import type { Persona } from "./types";

export default function App() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [users, setUsers] = useState<Persona[]>([]);
  const [selected, setSelected] = useState("");
  const [meta, setMeta] = useState({ mode: "…", env: "dev", configVersion: "" });

  useEffect(() => {
    fetchUsers().then((list) => {
      setUsers(list);
      if (list.length > 0) setSelected(list[0].sub);
    });
    fetchMeta().then(setMeta).catch(() => undefined);
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
      <Masthead users={users} selected={selected} onSelect={selectUser} mode={meta.mode} />
      <div className="cols">
        <div className="rail-left">
          <ContextCard context={state.context} />
          <AttestationCard report={state.attestation} />
          <ChecksCard
            attestationChecks={state.attestation ? state.attestation.clusters[0]?.checks.length ?? null : null}
            app360Checks={
              state.lastApp360
                ? state.lastApp360.sections.reduce((n, s) => n + s.checks.length, 0)
                : null
            }
            configVersion={meta.configVersion}
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
