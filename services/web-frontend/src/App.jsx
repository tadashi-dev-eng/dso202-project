// services/web-frontend/src/App.jsx -- TaskFlow single-page UI.
//
// One screen per concern, each backed by one service through the gateway:
//   lists sidebar ........ list-service      task table ....... task-service
//   tags ................. tag-service       comments ......... comment-service
//   files ................ attachment-service reminders ...... reminder-service
//   repeat ............... recurrence-service search .......... search-service
//   bell ................. notification-service stats ........ analytics-service
//   activity ............. audit-service     account .......... user-service
// Panels for optional services degrade quietly if that service is down.

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError, loadSession, login, logout, optional, register, setLogoutHandler } from "./api.js";

const PRIORITIES = ["low", "medium", "high"];
const STATUSES = { todo: "To do", in_progress: "In progress", done: "Done" };
const COLORS = ["#4f46e5", "#0891b2", "#059669", "#d97706", "#dc2626", "#7c3aed", "#db2777"];

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function toLocalInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// search-service marks matches with « and » (plain text, never HTML), so
// user-written content cannot inject markup into the page.
function Highlight({ text }) {
  return (text || "").split(/(«[^»]*»)/).map((part, i) =>
    part.startsWith("«") ? <mark key={i}>{part.slice(1, -1)}</mark> : <span key={i}>{part}</span>
  );
}

function isOverdue(task) {
  return task.due_at && task.status !== "done" && new Date(task.due_at) < new Date();
}

// --------------------------------------------------------------------------
// Authentication
// --------------------------------------------------------------------------
function AuthScreen({ onAuthed }) {
  const [mode, setMode] = useState("login");
  const [form, setForm] = useState({ email: "", password: "", name: "" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const s =
        mode === "login"
          ? await login(form.email, form.password)
          : await register(form.email, form.password, form.name);
      onAuthed(s);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  return (
    <div className="auth">
      <form className="auth-card" onSubmit={submit}>
        <div className="brand big">
          <span className="logo">✓</span> TaskFlow
        </div>
        <p className="muted">Fifteen microservices, one todo list.</p>
        {mode === "register" && (
          <label>
            Name
            <input value={form.name} onChange={set("name")} required maxLength={80} autoFocus />
          </label>
        )}
        <label>
          E-mail
          <input type="email" value={form.email} onChange={set("email")} required autoFocus={mode === "login"} />
        </label>
        <label>
          Password
          <input type="password" value={form.password} onChange={set("password")} required minLength={8} />
        </label>
        {error && <div className="error">{error}</div>}
        <button className="primary" disabled={busy}>
          {busy ? "…" : mode === "login" ? "Sign in" : "Create account"}
        </button>
        <button type="button" className="link" onClick={() => setMode(mode === "login" ? "register" : "login")}>
          {mode === "login" ? "No account? Register" : "Have an account? Sign in"}
        </button>
      </form>
    </div>
  );
}

// --------------------------------------------------------------------------
// Sidebar: lists
// --------------------------------------------------------------------------
function Sidebar({ lists, selected, onSelect, onCreate, view, setView, unread }) {
  const [name, setName] = useState("");
  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="logo">✓</span> TaskFlow
      </div>
      <nav>
        <button className={view === "all" ? "nav active" : "nav"} onClick={() => setView("all")}>
          All tasks
        </button>
        <button className={view === "notifications" ? "nav active" : "nav"} onClick={() => setView("notifications")}>
          Notifications {unread > 0 && <span className="badge">{unread}</span>}
        </button>
        <button className={view === "stats" ? "nav active" : "nav"} onClick={() => setView("stats")}>
          Statistics
        </button>
        <button className={view === "account" ? "nav active" : "nav"} onClick={() => setView("account")}>
          Account
        </button>
      </nav>
      <div className="section-title">Lists</div>
      <ul className="lists">
        {lists.map((l) => (
          <li key={l.id}>
            <button
              className={view === "list" && selected === l.id ? "nav active" : "nav"}
              onClick={() => onSelect(l.id)}
            >
              <span className="dot" style={{ background: l.color }} />
              <span className="grow">{l.name}</span>
              {l.role !== "owner" && <span className="pill small">{l.role}</span>}
            </button>
          </li>
        ))}
      </ul>
      <form
        className="new-list"
        onSubmit={(e) => {
          e.preventDefault();
          if (name.trim()) onCreate(name.trim()).then(() => setName(""));
        }}
      >
        <input placeholder="New list…" value={name} onChange={(e) => setName(e.target.value)} maxLength={100} />
      </form>
    </aside>
  );
}

// --------------------------------------------------------------------------
// Task table
// --------------------------------------------------------------------------
function NewTaskForm({ listId, onCreated }) {
  const [title, setTitle] = useState("");
  const [priority, setPriority] = useState("medium");
  const [due, setDue] = useState("");
  const [error, setError] = useState("");

  async function submit(e) {
    e.preventDefault();
    if (!title.trim()) return;
    try {
      const task = await api("/tasks", {
        method: "POST",
        body: { list_id: listId, title: title.trim(), priority, due_at: due ? new Date(due).toISOString() : null },
      });
      setTitle("");
      setDue("");
      setError("");
      onCreated(task);
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <form className="new-task" onSubmit={submit}>
      <input placeholder="Add a task…" value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} />
      <select value={priority} onChange={(e) => setPriority(e.target.value)} aria-label="Priority">
        {PRIORITIES.map((p) => (
          <option key={p}>{p}</option>
        ))}
      </select>
      <input type="datetime-local" value={due} onChange={(e) => setDue(e.target.value)} aria-label="Due" />
      <button className="primary">Add</button>
      {error && <div className="error full">{error}</div>}
    </form>
  );
}

function TaskRow({ task, list, tags, onToggle, onOpen, active }) {
  return (
    <li className={`task ${task.status} ${active ? "active" : ""}`} onClick={() => onOpen(task)}>
      <input
        type="checkbox"
        checked={task.status === "done"}
        onClick={(e) => e.stopPropagation()}
        onChange={() => onToggle(task)}
        aria-label="Done"
      />
      <div className="grow">
        <div className="title">{task.title}</div>
        <div className="meta">
          {list && (
            <span className="list-tag">
              <span className="dot" style={{ background: list.color }} />
              {list.name}
            </span>
          )}
          <span className={`prio ${task.priority}`}>{task.priority}</span>
          {task.status === "in_progress" && <span className="pill">in progress</span>}
          {task.due_at && <span className={isOverdue(task) ? "due overdue" : "due"}>⏰ {fmtDate(task.due_at)}</span>}
          {task.recurrence_of && <span className="pill">↻ repeats</span>}
          {(tags || []).map((t) => (
            <span key={t.id} className="tag" style={{ borderColor: t.color, color: t.color }}>
              {t.name}
            </span>
          ))}
        </div>
      </div>
    </li>
  );
}

// --------------------------------------------------------------------------
// Task detail panel (details, tags, comments, files, reminders, repeat, activity)
// --------------------------------------------------------------------------
function TaskDetail({ task, canWrite, allTags, onChanged, onDeleted, onClose, onTagsChanged }) {
  const [tab, setTab] = useState("details");
  const [draft, setDraft] = useState(task);
  useEffect(() => setDraft(task), [task]);

  async function save(fields) {
    const updated = await api(`/tasks/${task.id}`, { method: "PATCH", body: fields });
    onChanged(updated);
  }

  return (
    <aside className="detail">
      <header>
        <select
          value={draft.status}
          disabled={!canWrite}
          onChange={(e) => save({ status: e.target.value })}
          aria-label="Status"
        >
          {Object.entries(STATUSES).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </select>
        <button className="icon" onClick={onClose} aria-label="Close">
          ✕
        </button>
      </header>
      <input
        className="detail-title"
        value={draft.title}
        disabled={!canWrite}
        onChange={(e) => setDraft({ ...draft, title: e.target.value })}
        onBlur={() => draft.title !== task.title && draft.title.trim() && save({ title: draft.title.trim() })}
      />
      <div className="tabs">
        {["details", "comments", "files", "reminders", "activity"].map((t) => (
          <button key={t} className={tab === t ? "tab active" : "tab"} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </div>
      {tab === "details" && (
        <div className="tab-body">
          <label>
            Priority
            <select value={draft.priority} disabled={!canWrite} onChange={(e) => save({ priority: e.target.value })}>
              {PRIORITIES.map((p) => (
                <option key={p}>{p}</option>
              ))}
            </select>
          </label>
          <label>
            Due
            <input
              type="datetime-local"
              disabled={!canWrite}
              value={toLocalInput(draft.due_at)}
              onChange={(e) =>
                save(e.target.value ? { due_at: new Date(e.target.value).toISOString() } : { clear_due: true })
              }
            />
          </label>
          <label>
            Description
            <textarea
              rows={5}
              disabled={!canWrite}
              value={draft.description}
              onChange={(e) => setDraft({ ...draft, description: e.target.value })}
              onBlur={() => draft.description !== task.description && save({ description: draft.description })}
            />
          </label>
          <TagPicker taskId={task.id} allTags={allTags} canWrite={canWrite} onChanged={onTagsChanged} />
          <Recurrence taskId={task.id} canWrite={canWrite} />
          <p className="muted small">
            Created {fmtDate(task.created_at)}
            {task.completed_at && ` · completed ${fmtDate(task.completed_at)}`}
          </p>
          {canWrite && (
            <button
              className="danger"
              onClick={async () => {
                await api(`/tasks/${task.id}`, { method: "DELETE" });
                onDeleted(task);
              }}
            >
              Delete task
            </button>
          )}
        </div>
      )}
      {tab === "comments" && <Comments taskId={task.id} />}
      {tab === "files" && <Attachments taskId={task.id} canWrite={canWrite} />}
      {tab === "reminders" && <Reminders taskId={task.id} />}
      {tab === "activity" && <Activity taskId={task.id} />}
    </aside>
  );
}

function TagPicker({ taskId, allTags, canWrite, onChanged }) {
  const [assigned, setAssigned] = useState(null);
  const [name, setName] = useState("");
  useEffect(() => {
    optional(`/tags/task/${taskId}`, null).then(setAssigned);
  }, [taskId]);
  if (assigned === null) return null; // tag-service unavailable

  async function toggle(tag) {
    const ids = assigned.some((t) => t.id === tag.id)
      ? assigned.filter((t) => t.id !== tag.id).map((t) => t.id)
      : [...assigned.map((t) => t.id), tag.id];
    const next = await api(`/tags/task/${taskId}`, { method: "PUT", body: { tag_ids: ids } });
    setAssigned(next);
    onChanged();
  }

  async function create(e) {
    e.preventDefault();
    if (!name.trim()) return;
    const tag = await api("/tags", {
      method: "POST",
      body: { name: name.trim(), color: COLORS[Math.floor(Math.random() * COLORS.length)] },
    });
    setName("");
    await onChanged();
    await toggle(tag);
  }

  return (
    <div className="field">
      <span className="label">Tags</span>
      <div className="tag-cloud">
        {allTags.map((t) => {
          const on = assigned.some((a) => a.id === t.id);
          return (
            <button
              key={t.id}
              className={on ? "tag on" : "tag"}
              disabled={!canWrite}
              style={on ? { background: t.color, borderColor: t.color } : { borderColor: t.color, color: t.color }}
              onClick={() => toggle(t)}
            >
              {t.name}
            </button>
          );
        })}
        {canWrite && (
          <form onSubmit={create} className="inline">
            <input placeholder="+ tag" value={name} onChange={(e) => setName(e.target.value)} maxLength={30} />
          </form>
        )}
      </div>
    </div>
  );
}

function Recurrence({ taskId, canWrite }) {
  // undefined = loading or service unavailable (hide); null = no rule yet.
  const [rule, setRule] = useState(undefined);
  useEffect(() => {
    optional(`/recurrences/task/${taskId}`, "unavailable").then((r) =>
      setRule(r === "unavailable" ? undefined : r)
    );
  }, [taskId]);
  if (rule === undefined) return null;

  async function change(value) {
    if (value === "none") {
      await api(`/recurrences/task/${taskId}`, { method: "DELETE" });
      setRule(null);
    } else {
      setRule(await api(`/recurrences/task/${taskId}`, { method: "PUT", body: { frequency: value } }));
    }
  }

  return (
    <label>
      Repeat when completed
      <select value={rule?.frequency || "none"} disabled={!canWrite} onChange={(e) => change(e.target.value)}>
        <option value="none">never</option>
        <option value="daily">daily</option>
        <option value="weekly">weekly</option>
        <option value="monthly">monthly</option>
      </select>
    </label>
  );
}

function Comments({ taskId }) {
  const [items, setItems] = useState(null);
  const [body, setBody] = useState("");
  const load = useCallback(() => optional(`/comments?task_id=${taskId}`, null).then(setItems), [taskId]);
  useEffect(() => {
    load();
  }, [load]);
  if (items === null) return <div className="tab-body muted">Comments are unavailable right now.</div>;
  return (
    <div className="tab-body">
      <ul className="feed">
        {items.map((c) => (
          <li key={c.id}>
            <div className="feed-head">
              <strong>{c.author_name || "someone"}</strong> <span className="muted small">{fmtDate(c.created_at)}</span>
            </div>
            <div className="feed-body">{c.body}</div>
          </li>
        ))}
        {items.length === 0 && <li className="muted">No comments yet.</li>}
      </ul>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          if (!body.trim()) return;
          await api("/comments", { method: "POST", body: { task_id: taskId, body: body.trim() } });
          setBody("");
          load();
        }}
      >
        <textarea rows={3} placeholder="Write a comment…" value={body} onChange={(e) => setBody(e.target.value)} />
        <button className="primary">Comment</button>
      </form>
    </div>
  );
}

function Attachments({ taskId, canWrite }) {
  const [items, setItems] = useState(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => optional(`/attachments?task_id=${taskId}`, null).then(setItems), [taskId]);
  useEffect(() => {
    load();
  }, [load]);
  if (items === null) return <div className="tab-body muted">Attachments are unavailable right now.</div>;

  async function download(a) {
    const resp = await api(`/attachments/${a.id}/download`, { raw: true });
    const url = URL.createObjectURL(await resp.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = a.filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  return (
    <div className="tab-body">
      <ul className="files">
        {items.map((a) => (
          <li key={a.id}>
            <button className="link" onClick={() => download(a)}>
              📎 {a.filename}
            </button>
            <span className="muted small">{Math.max(1, Math.round(a.size / 1024))} KB</span>
            {canWrite && (
              <button
                className="icon"
                aria-label="Delete"
                onClick={async () => {
                  await api(`/attachments/${a.id}`, { method: "DELETE" });
                  load();
                }}
              >
                ✕
              </button>
            )}
          </li>
        ))}
        {items.length === 0 && <li className="muted">No files.</li>}
      </ul>
      {canWrite && (
        <label className="upload">
          {busy ? "Uploading…" : "Upload a file (max 10 MB)"}
          <input
            type="file"
            disabled={busy}
            onChange={async (e) => {
              const file = e.target.files[0];
              if (!file) return;
              setBusy(true);
              const form = new FormData();
              form.append("file", file);
              try {
                await api(`/attachments?task_id=${taskId}`, { method: "POST", body: form });
                load();
              } finally {
                setBusy(false);
                e.target.value = "";
              }
            }}
          />
        </label>
      )}
    </div>
  );
}

function Reminders({ taskId }) {
  const [items, setItems] = useState(null);
  const [when, setWhen] = useState("");
  const load = useCallback(() => optional(`/reminders?task_id=${taskId}`, null).then(setItems), [taskId]);
  useEffect(() => {
    load();
  }, [load]);
  if (items === null) return <div className="tab-body muted">Reminders are unavailable right now.</div>;
  return (
    <div className="tab-body">
      <ul className="files">
        {items.map((r) => (
          <li key={r.id}>
            <span>⏰ {fmtDate(r.remind_at)}</span>
            <span className="pill small">{r.status}</span>
            <button
              className="icon"
              aria-label="Delete"
              onClick={async () => {
                await api(`/reminders/${r.id}`, { method: "DELETE" });
                load();
              }}
            >
              ✕
            </button>
          </li>
        ))}
        {items.length === 0 && <li className="muted">No reminders.</li>}
      </ul>
      <form
        className="inline"
        onSubmit={async (e) => {
          e.preventDefault();
          if (!when) return;
          await api("/reminders", {
            method: "POST",
            body: { task_id: taskId, remind_at: new Date(when).toISOString() },
          });
          setWhen("");
          load();
        }}
      >
        <input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} />
        <button className="primary">Remind me</button>
      </form>
    </div>
  );
}

function Activity({ taskId }) {
  const [items, setItems] = useState(null);
  useEffect(() => {
    optional(`/audit?entity_id=${taskId}`, null).then(setItems);
  }, [taskId]);
  if (items === null) return <div className="tab-body muted">Activity is unavailable right now.</div>;
  return (
    <div className="tab-body">
      <ul className="feed">
        {items.map((e) => (
          <li key={e.id}>
            <div className="feed-head">
              <code>{e.event_type}</code> <span className="muted small">{fmtDate(e.occurred_at)}</span>
            </div>
            <div className="muted small">by {e.actor_name || e.actor_id || "system"}</div>
          </li>
        ))}
        {items.length === 0 && <li className="muted">No activity recorded.</li>}
      </ul>
    </div>
  );
}

// --------------------------------------------------------------------------
// Other views
// --------------------------------------------------------------------------
function ShareDialog({ list, onClose }) {
  const [detail, setDetail] = useState(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("editor");
  const [error, setError] = useState("");
  const load = useCallback(() => api(`/lists/${list.id}`).then(setDetail), [list.id]);
  useEffect(() => {
    load();
  }, [load]);
  return (
    <div className="modal" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <h3>Share “{list.name}”</h3>
        <ul className="files">
          {detail?.members.map((m) => (
            <li key={m.user_id}>
              <code className="small">{m.user_id.slice(0, 8)}</code>
              <span className="pill small">{m.role}</span>
              {m.role !== "owner" && (
                <button
                  className="icon"
                  onClick={async () => {
                    await api(`/lists/${list.id}/members/${m.user_id}`, { method: "DELETE" });
                    load();
                  }}
                >
                  ✕
                </button>
              )}
            </li>
          ))}
        </ul>
        <form
          className="inline"
          onSubmit={async (e) => {
            e.preventDefault();
            try {
              await api(`/lists/${list.id}/members`, { method: "POST", body: { email, role } });
              setEmail("");
              setError("");
              load();
            } catch (err) {
              setError(err.message);
            }
          }}
        >
          <input type="email" placeholder="colleague@example.com" value={email} onChange={(e) => setEmail(e.target.value)} required />
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            <option>editor</option>
            <option>viewer</option>
          </select>
          <button className="primary">Add</button>
        </form>
        {error && <div className="error">{error}</div>}
        <button className="link" onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  );
}

function Notifications({ items, reload }) {
  return (
    <section className="main-body">
      <div className="toolbar">
        <h2>Notifications</h2>
        <button
          className="link"
          onClick={async () => {
            await optional("/notifications/read-all", null, { method: "POST" });
            reload();
          }}
        >
          Mark all read
        </button>
      </div>
      {items === null ? (
        <p className="muted">notification-service is unavailable right now.</p>
      ) : (
        <ul className="feed">
          {items.map((n) => (
            <li key={n.id} className={n.read ? "" : "unread"}>
              <div className="feed-head">
                <strong>{n.title}</strong> <span className="muted small">{fmtDate(n.created_at)}</span>
              </div>
              <div className="feed-body">{n.body}</div>
              <div className="muted small">via {n.channel}</div>
            </li>
          ))}
          {items.length === 0 && <li className="muted">Nothing yet.</li>}
        </ul>
      )}
    </section>
  );
}

function Stats() {
  const [stats, setStats] = useState(undefined);
  useEffect(() => {
    optional("/stats/me", null).then(setStats);
  }, []);
  if (stats === undefined) return <section className="main-body muted">Loading…</section>;
  if (stats === null)
    return <section className="main-body muted">analytics-service is unavailable right now.</section>;
  const max = Math.max(1, ...stats.last_7_days.map((d) => Math.max(d.created, d.completed)));
  return (
    <section className="main-body">
      <h2>Your productivity</h2>
      <div className="tiles">
        <div className="tile">
          <div className="tile-num">{stats.created}</div>
          <div className="muted">tasks created</div>
        </div>
        <div className="tile">
          <div className="tile-num">{stats.completed}</div>
          <div className="muted">tasks completed</div>
        </div>
        <div className="tile">
          <div className="tile-num">{stats.completion_rate}%</div>
          <div className="muted">completion rate</div>
        </div>
        <div className="tile">
          <div className="tile-num">{stats.comments}</div>
          <div className="muted">comments written</div>
        </div>
      </div>
      <h3>Last 7 days</h3>
      <div className="bars" role="img" aria-label="Tasks created and completed per day">
        {stats.last_7_days.map((d) => (
          <div key={d.day} className="bar-group">
            <div className="bar-pair">
              <div className="bar created" style={{ height: `${(d.created / max) * 100}%` }} title={`${d.created} created`} />
              <div className="bar completed" style={{ height: `${(d.completed / max) * 100}%` }} title={`${d.completed} completed`} />
            </div>
            <div className="muted small">{d.day.slice(5)}</div>
          </div>
        ))}
      </div>
      <p className="legend">
        <span className="swatch created" /> created <span className="swatch completed" /> completed
      </p>
    </section>
  );
}

function Account({ me, onSaved }) {
  const [name, setName] = useState(me?.display_name || "");
  const [saved, setSaved] = useState(false);
  if (!me) return <section className="main-body muted">Loading…</section>;
  return (
    <section className="main-body">
      <h2>Account</h2>
      <form
        className="stack narrow"
        onSubmit={async (e) => {
          e.preventDefault();
          onSaved(await api("/users/me", { method: "PATCH", body: { display_name: name } }));
          setSaved(true);
        }}
      >
        <label>
          Display name
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={80} />
        </label>
        <label>
          E-mail
          <input value={me.email} disabled />
        </label>
        <label>
          Timezone
          <input value={me.timezone} disabled />
        </label>
        <button className="primary">Save</button>
        {saved && <span className="muted">Saved.</span>}
      </form>
      <button className="link" onClick={logout}>
        Sign out
      </button>
    </section>
  );
}

// --------------------------------------------------------------------------
// App shell
// --------------------------------------------------------------------------
export default function App() {
  const [session, setSession] = useState(loadSession());
  const [me, setMe] = useState(null);
  const [lists, setLists] = useState([]);
  const [view, setView] = useState("all");
  const [selected, setSelected] = useState(null);
  const [tasks, setTasks] = useState([]);
  const [open, setOpen] = useState(null);
  const [filter, setFilter] = useState("open");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState(null);
  const [tags, setTags] = useState([]);
  const [taskTags, setTaskTags] = useState({});
  const [notifications, setNotifications] = useState([]);
  const [sharing, setSharing] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => setLogoutHandler(() => setSession(null)), []);

  const listById = useMemo(() => Object.fromEntries(lists.map((l) => [l.id, l])), [lists]);

  const loadLists = useCallback(async () => {
    const ls = await api("/lists");
    setLists(ls);
    return ls;
  }, []);

  const loadTasks = useCallback(async () => {
    const path = view === "list" && selected ? `/tasks?list_id=${selected}` : "/tasks";
    const ts = await api(path);
    setTasks(ts);
    if (ts.length) {
      const map = await optional(`/tags/tasks?ids=${ts.map((t) => t.id).join(",")}`, {});
      setTaskTags(map);
    }
  }, [view, selected]);

  const loadTags = useCallback(() => optional("/tags", []).then(setTags), []);
  const loadNotifications = useCallback(() => optional("/notifications", null).then(setNotifications), []);

  // first load after sign-in
  useEffect(() => {
    if (!session) return;
    (async () => {
      try {
        setMe(await api("/users/me"));
        const ls = await loadLists();
        if (ls.length === 0) {
          const inbox = await api("/lists", { method: "POST", body: { name: "Inbox" } });
          setLists([inbox]);
        }
        loadTags();
        loadNotifications();
      } catch (err) {
        if (!(err instanceof ApiError && err.status === 401)) setError(err.message);
      }
    })();
  }, [session, loadLists, loadTags, loadNotifications]);

  useEffect(() => {
    if (session && (view === "all" || view === "list")) loadTasks().catch((e) => setError(e.message));
  }, [session, view, selected, loadTasks]);

  // poll the bell every 20 s
  useEffect(() => {
    if (!session) return undefined;
    const id = setInterval(loadNotifications, 20000);
    return () => clearInterval(id);
  }, [session, loadNotifications]);

  // debounced search
  useEffect(() => {
    if (!query.trim()) {
      setResults(null);
      return undefined;
    }
    const id = setTimeout(() => {
      optional(`/search?q=${encodeURIComponent(query.trim())}`, []).then(setResults);
    }, 300);
    return () => clearTimeout(id);
  }, [query]);

  if (!session) return <AuthScreen onAuthed={setSession} />;

  const visible = tasks.filter((t) => (filter === "open" ? t.status !== "done" : filter === "done" ? t.status === "done" : true));
  const current = view === "list" ? listById[selected] : null;
  const defaultList = current || lists.find((l) => l.role !== "viewer");
  const openList = open ? listById[open.list_id] : null;
  const unread = (notifications || []).filter((n) => !n.read).length;

  async function toggle(task) {
    const updated = await api(`/tasks/${task.id}`, {
      method: "PATCH",
      body: { status: task.status === "done" ? "todo" : "done" },
    });
    replace(updated);
  }

  function replace(updated) {
    setTasks((ts) => ts.map((t) => (t.id === updated.id ? updated : t)));
    if (open?.id === updated.id) setOpen(updated);
  }

  return (
    <div className="app">
      <Sidebar
        lists={lists}
        selected={selected}
        view={view}
        unread={unread}
        setView={(v) => {
          setView(v);
          setOpen(null);
        }}
        onSelect={(id) => {
          setSelected(id);
          setView("list");
          setOpen(null);
        }}
        onCreate={async (name) => {
          const l = await api("/lists", {
            method: "POST",
            body: { name, color: COLORS[lists.length % COLORS.length] },
          });
          setLists([...lists, l]);
          setSelected(l.id);
          setView("list");
        }}
      />
      <main>
        <header className="topbar">
          <div className="search">
            <input placeholder="Search tasks and comments…" value={query} onChange={(e) => setQuery(e.target.value)} />
            {results && (
              <ul className="results">
                {results.length === 0 && <li className="muted">No matches.</li>}
                {results.map((r) => (
                  <li
                    key={`${r.kind}-${r.id}`}
                    onClick={async () => {
                      setQuery("");
                      try {
                        const t = await api(`/tasks/${r.task_id}`);
                        setSelected(t.list_id);
                        setView("list");
                        setOpen(t);
                      } catch {
                        /* deleted since indexing */
                      }
                    }}
                  >
                    <span className="pill small">{r.kind}</span> <Highlight text={r.snippet} />
                  </li>
                ))}
              </ul>
            )}
          </div>
          <span className="muted">{me?.display_name}</span>
        </header>
        {error && (
          <div className="error banner" onClick={() => setError("")}>
            {error}
          </div>
        )}
        {(view === "all" || view === "list") && (
          <section className="main-body">
            <div className="toolbar">
              <h2>
                {current ? (
                  <>
                    <span className="dot big" style={{ background: current.color }} />
                    {current.name}
                  </>
                ) : (
                  "All tasks"
                )}
              </h2>
              <div className="toolbar-actions">
                <div className="seg">
                  {["open", "done", "all"].map((f) => (
                    <button key={f} className={filter === f ? "on" : ""} onClick={() => setFilter(f)}>
                      {f}
                    </button>
                  ))}
                </div>
                {current?.role === "owner" && (
                  <>
                    <button className="link" onClick={() => setSharing(current)}>
                      Share
                    </button>
                    <button
                      className="link danger-text"
                      onClick={async () => {
                        if (!window.confirm(`Delete list “${current.name}” and its tasks?`)) return;
                        await api(`/lists/${current.id}`, { method: "DELETE" });
                        await loadLists();
                        setView("all");
                      }}
                    >
                      Delete list
                    </button>
                  </>
                )}
              </div>
            </div>
            {defaultList && current?.role !== "viewer" && (
              <NewTaskForm listId={defaultList.id} onCreated={(t) => setTasks([t, ...tasks])} />
            )}
            <ul className="tasks">
              {visible.map((t) => (
                <TaskRow
                  key={t.id}
                  task={t}
                  list={view === "all" ? listById[t.list_id] : null}
                  tags={taskTags[t.id]}
                  active={open?.id === t.id}
                  onToggle={toggle}
                  onOpen={setOpen}
                />
              ))}
              {visible.length === 0 && <li className="empty muted">Nothing here. Add a task above.</li>}
            </ul>
          </section>
        )}
        {view === "notifications" && <Notifications items={notifications} reload={loadNotifications} />}
        {view === "stats" && <Stats />}
        {view === "account" && <Account me={me} onSaved={setMe} />}
      </main>
      {open && (
        <TaskDetail
          task={open}
          canWrite={openList ? openList.role !== "viewer" : false}
          allTags={tags}
          onChanged={replace}
          onDeleted={(t) => {
            setTasks(tasks.filter((x) => x.id !== t.id));
            setOpen(null);
          }}
          onClose={() => setOpen(null)}
          onTagsChanged={async () => {
            await loadTags();
            await loadTasks();
          }}
        />
      )}
      {sharing && <ShareDialog list={sharing} onClose={() => setSharing(null)} />}
    </div>
  );
}
