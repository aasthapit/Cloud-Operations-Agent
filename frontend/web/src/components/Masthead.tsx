/** Masthead: product identity + the dev identity picker (FR-UI-4). */
import type { Persona } from "../types";

export function Masthead(props: {
  users: Persona[];
  selected: string;
  onSelect: (sub: string) => void;
  mode: string;
  /** "dev" shows the persona picker; "oidc" shows the verified identity. */
  authMode?: string;
  me?: Persona | null;
}) {
  const current = props.authMode === "oidc" ? props.me : props.users.find((u) => u.sub === props.selected);
  const initials = (current?.name ?? "?")
    .split(/\s+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
  return (
    <header className="masthead">
      <span className="title">Cloud Operations Agent</span>
      <span className="id">
        <span className="pill dim">{props.mode} mode</span>
        {props.authMode === "oidc" && (
          <span className="pill dim" title={current?.email ?? ""}>
            {current ? `${current.name || current.sub}` : "sign-in required"}
          </span>
        )}
        {props.authMode !== "oidc" && props.users.length > 0 && (
          <>
            <span className="pill dim">dev identity</span>
            <select
              value={props.selected}
              onChange={(e) => props.onSelect(e.target.value)}
              aria-label="Dev identity picker"
            >
              {props.users.map((u) => (
                <option key={u.sub} value={u.sub}>
                  {u.name} - {u.groups.join(", ")}
                </option>
              ))}
            </select>
          </>
        )}
        <span className="avatar">{initials}</span>
      </span>
    </header>
  );
}
