// 한국어: 프로젝트 위키 패널 — 위키 카드에서 열 때만 개요를 읽고, 설정·복사는 미리보기 계획 하나를 확인한 뒤에만 적용하며, 본문 검색 결과를 파일·줄로 보인다. 프로젝트가 바뀌거나 단계가 지난 뒤 온 응답은 버린다.
import { useEffect, useRef, useState, type FormEvent } from "react";
import type {
  ErrorDto,
  Progress,
  ProjectWikiDto,
  TaskEvent,
  TaskId,
  ToolsBackupDto,
  WikiActionReq,
  WikiApplyDto,
  WikiApplyReq,
  WikiCandidateDto,
  WikiCopyBlock,
  WikiFileAction,
  WikiItemStatus,
  WikiPlanDto,
  WikiSearchDto,
  WikiSkipReason,
} from "./contract";
import { showValue } from "./project-tools-settings";
import { toErrorDto, type Transport } from "./transport/types";

/** The preview the user is looking at and the request it answers; an apply is built from this alone. */
interface Review {
  readonly request: WikiActionReq;
  readonly plan: WikiPlanDto;
  /** The separate acknowledgement of this plan's settings upgrade; it never carries over to another plan. */
  readonly acknowledged: boolean;
}

type Retry = { readonly kind: "overview" } | { readonly kind: "preview"; readonly request: WikiActionReq };

interface Failure {
  readonly lead: string;
  readonly error: ErrorDto | null;
  readonly retry: Retry | null;
}

type Phase =
  | { readonly name: "overview" }
  | { readonly name: "review"; readonly review: Review }
  | {
      readonly name: "apply";
      readonly review: Review;
      /** Known once the start answered; Stop needs it. */
      readonly taskId: TaskId | null;
      readonly progress: Progress | null;
      readonly stopping: boolean;
    }
  | { readonly name: "result"; readonly review: Review; readonly result: WikiApplyDto };

interface PanelState {
  readonly overview: ProjectWikiDto | null;
  /** The folder the user picked among the ones found; `null` leaves the choice to core. */
  readonly choice: string | null;
  readonly busy: "overview" | "preview" | null;
  readonly phase: Phase;
  readonly failure: Failure | null;
}

interface SearchState {
  readonly query: string;
  readonly busy: boolean;
  /** The text the shown result is for. */
  readonly searched: string;
  readonly result: WikiSearchDto | null;
  readonly error: ErrorDto | null;
  readonly note: string | null;
}

const OVERVIEW: Phase = { name: "overview" };
const INITIAL: PanelState = { overview: null, choice: null, busy: "overview", phase: OVERVIEW, failure: null };
const NO_SEARCH: SearchState = { query: "", busy: false, searched: "", result: null, error: null, note: null };
const TITLE_ID = "project-wiki-title";
const ACK_ID = "project-wiki-upgrade-ack";
const QUERY_ID = "project-wiki-query";

/** The codes that mean the reviewed plan no longer matches the project; a new preview is the only way on. */
const STALE_CODES = new Set(["OAH_PLAN_STALE", "OAH_CONFIG_CHANGED", "OAH_CONFIG_UPGRADE_REQUIRED"]);
const STOPPED = "OAH_WIKI_STOPPED";

const FILE_ACTION: Record<WikiFileAction, string> = {
  create: "Create",
  keep: "Keep: already there, not changed",
  blocked: "Blocked: the name is taken by something that is not a file",
  copy: "Copy",
  same: "Keep: the same contents are already there",
};

const BLOCK_REASON: Record<WikiCopyBlock, string> = {
  link: "It is a link, or it is on another disk.",
  "not-portable": "The name does not work on every system.",
  "case-collision": "Two names differ only by upper and lower case.",
  oversized: "It is over the size limit.",
  "too-deep": "It is too many folders deep.",
  unreadable: "It cannot be read.",
  conflict: "The destination has something else under this name, and nothing is replaced.",
  "link-leaves-wiki": "A link in it leaves the wiki folder and would point somewhere else from the new place.",
};

const SKIP_REASON: Record<WikiSkipReason, string> = {
  link: "a link",
  "not-portable": "a name that does not work on every system",
  oversized: "too large",
  "not-text": "not text",
  unreadable: "cannot be read",
  "too-deep": "too many folders deep",
};

const ITEM_STATUS: Record<WikiItemStatus, string> = {
  created: "Created",
  kept: "Kept as it was",
  failed: "Failed",
  pending: "Not started",
};

const KEY_LABEL: Record<string, string> = {
  "wiki.dir": "Wiki folder",
  "impact.wiki_dirs": "Wiki folders the Impact Map reads",
};

const KEPT_REASON: Record<string, string> = {
  "explicit-wiki-dirs": "The Impact Map has its own list of wiki folders, so the new wiki folder does not replace it.",
};

const BACKUP_TEXT: Record<ToolsBackupDto["state"], string> = {
  write: "The file as it is now will be kept as",
  reuse: "A backup with the same contents already exists and is kept:",
  conflict: "Another file is already at the backup path. Move it away, then preview again:",
};

const PROGRESS_TEXT: Record<string, string> = {
  verify: "Checking that nothing changed since the preview…",
  "write-files": "Writing files…",
  "save-settings": "Saving the setting…",
};

const WHOLE = new Intl.NumberFormat();
const BYTES = new Intl.NumberFormat(undefined, { style: "unit", unit: "byte", unitDisplay: "long" });
const KILOBYTES = new Intl.NumberFormat(undefined, { style: "unit", unit: "kilobyte", maximumFractionDigits: 1 });
const MEGABYTES = new Intl.NumberFormat(undefined, { style: "unit", unit: "megabyte", maximumFractionDigits: 1 });

export function showBytes(bytes: number): string {
  if (bytes < 1024) return BYTES.format(bytes);
  if (bytes < 1024 * 1024) return KILOBYTES.format(bytes / 1024);
  return MEGABYTES.format(bytes / (1024 * 1024));
}

function counted(count: number, one: string, many: string): string {
  return WHOLE.format(count) + " " + (count === 1 ? one : many);
}

function wrongProject(): ErrorDto {
  return {
    code: "UI_PROJECT_CHANGED",
    message: "The answer was for another project, so it is not used.",
    hint: null,
  };
}

/** Why a plan cannot be applied as it is, or "ready". */
export function gateOf(plan: WikiPlanDto): "ready" | "blocked" | "nothing" | "backup-conflict" {
  if (plan.blocked_total > 0 || plan.files.some((file) => file.action === "blocked")) return "blocked";
  if (plan.writes === 0 && (plan.settings?.changes.length ?? 0) === 0) return "nothing";
  if (plan.settings?.backup?.state === "conflict") return "backup-conflict";
  return "ready";
}

/** The native request, built from the reviewed plan alone: its request with its date, its ID, its acknowledged upgrade. */
export function wikiApplyRequestFor(review: Review): WikiApplyReq {
  const { plan } = review;
  const request: WikiActionReq = review.request.action === "setup" ? { ...review.request, today: plan.today } : review.request;
  const req: WikiApplyReq = { request, plan_id: plan.plan_id };
  if (plan.settings?.upgrade && review.acknowledged) req.approve_upgrade = plan.settings.upgrade.fingerprint;
  return req;
}

/** The folder the buttons act on: the user's pick, else the only folder found, else the folder in use when it was found. */
function pickedOf(overview: ProjectWikiDto, choice: string | null): string | null {
  if (choice !== null) return choice;
  const only = overview.candidates.length === 1 ? overview.candidates[0] : undefined;
  if (only) return only.dir;
  const inUse = overview.target.dir;
  return inUse !== null && overview.candidates.some((candidate) => candidate.dir === inUse) ? inUse : null;
}

function traitsOf(candidate: WikiCandidateDto): string {
  const traits: string[] = [];
  if (candidate.standard) traits.push("a standard location");
  if (candidate.obsidian) traits.push("an Obsidian vault");
  if (candidate.index) traits.push("has index.md");
  if (candidate.log) traits.push("has log.md");
  return traits.join(", ");
}

function ErrorText({ error }: { error: ErrorDto }) {
  return (
    <span>
      {error.code}: {error.message}
      {error.hint ? " " + error.hint : ""}
    </span>
  );
}

function Dir({ children }: { children: string }) {
  return <code translate="no">{children}</code>;
}

function TargetText({ overview }: { overview: ProjectWikiDto }) {
  const { target, candidates, configured } = overview;
  const dir = target.dir ?? "";
  switch (target.state) {
    case "ready":
      return (
        <p>
          The wiki is in <Dir>{dir}</Dir> and has every standard file.
        </p>
      );
    case "incomplete":
      return (
        <p>
          The wiki folder is <Dir>{dir}</Dir>.
          {target.creates > 0
            ? " Setup adds " + counted(target.creates, "standard file", "standard files") + " it does not have and changes none that are there."
            : ""}
          {target.sets_dir ? " Setup saves this folder in the project's settings." : ""}
        </p>
      );
    case "none":
      return (
        <p>
          No wiki folder was found. Setup creates <Dir>{dir}</Dir> with {counted(target.creates, "standard file", "standard files")}.
        </p>
      );
    case "choose":
      return <p>More than one folder looks like the wiki. Choose the one to use.</p>;
    case "configured-missing":
      return (
        <p>
          The settings name <Dir>{configured?.dir ?? dir}</Dir>, which is not in the project. Nothing is created in its place.{" "}
          {candidates.length > 0
            ? "Choose a folder that was found, or change the folder in the wiki settings."
            : "Create that folder, or change the folder in the wiki settings."}
        </p>
      );
    case "unavailable":
      return (
        <p className="status-error">
          The wiki folder cannot be used. {target.error ? <ErrorText error={target.error} /> : null}
        </p>
      );
  }
}

function OverviewView({
  overview,
  picked,
  onChoose,
}: {
  overview: ProjectWikiDto;
  picked: string | null;
  onChoose: (dir: string) => void;
}) {
  const { candidates, problems } = overview;
  const only = candidates.length === 1 ? candidates[0] : undefined;
  return (
    <div className="tool-review">
      <TargetText overview={overview} />
      {overview.settings_error ? (
        <p className="status-error">
          The project's settings could not be read. <ErrorText error={overview.settings_error} />
        </p>
      ) : null}
      {only ? (
        <p>
          Found: <Dir>{only.dir}</Dir> <span className="muted">({traitsOf(only)})</span>
        </p>
      ) : null}
      {candidates.length > 1 ? (
        <fieldset className="wiki-choice">
          <legend>Folders that look like a wiki</legend>
          {candidates.map((candidate) => (
            <label className="tool-check" key={candidate.dir}>
              <input
                type="radio"
                name="project-wiki-folder"
                value={candidate.dir}
                checked={picked === candidate.dir}
                onChange={() => onChoose(candidate.dir)}
              />
              <span>
                <Dir>{candidate.dir}</Dir> <span className="muted">({traitsOf(candidate)})</span>
              </span>
            </label>
          ))}
        </fieldset>
      ) : null}
      {problems.length > 0 ? (
        <div className="wiki-block">
          <p>
            <strong>Cannot be used</strong>
          </p>
          <ul>
            {problems.map((problem) => (
              <li key={problem.dir}>
                <Dir>{problem.dir}</Dir>: <ErrorText error={problem.error} />
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {overview.truncated ? (
        <p className="muted">The project has too many folders to look through all of them, so a wiki folder may be missing here.</p>
      ) : null}
    </div>
  );
}

function PlanLead({ plan }: { plan: WikiPlanDto }) {
  if (plan.mode === "copy") {
    return (
      <p>
        Copies <Dir>{plan.source ?? ""}</Dir> to <Dir>{plan.dir}</Dir>. <Dir>{plan.source ?? ""}</Dir> stays as it is, and nothing
        in <Dir>{plan.dir}</Dir> is replaced.
      </p>
    );
  }
  if (plan.mode === "create") {
    return (
      <p>
        Creates the wiki folder <Dir>{plan.dir}</Dir> with the standard files.
      </p>
    );
  }
  return (
    <p>
      Uses <Dir>{plan.dir}</Dir> where it is and adds the standard files it does not have. Files that are there are not
      changed.
    </p>
  );
}

function PlanSettings({ review, acknowledge }: { review: Review; acknowledge: (value: boolean) => void }) {
  const { plan } = review;
  const { settings } = plan;
  if (!settings) {
    return <p>The project's settings are not changed.</p>;
  }
  return (
    <div className="wiki-block">
      <h5>Then the Project's Settings</h5>
      <p>
        File: <Dir>{settings.config_path}</Dir> <span className="muted">(in the project)</span>. It is saved only after every
        file above is in place.
      </p>
      <table className="tool-plan">
        <caption>What saving changes</caption>
        <thead>
          <tr>
            <th scope="col">Setting</th>
            <th scope="col">Before</th>
            <th scope="col">After</th>
          </tr>
        </thead>
        <tbody>
          {settings.changes.map((change) => (
            <tr key={change.key} data-key={change.key}>
              <th scope="row">
                {KEY_LABEL[change.key] ? <span>{KEY_LABEL[change.key]} </span> : null}
                <code translate="no">{change.key}</code>
              </th>
              <td translate="no">{showValue(change.before)}</td>
              <td translate="no">{showValue(change.after)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {settings.kept.length > 0 ? (
        <div className="tool-kept">
          <p>
            <strong>Left as they are</strong>
          </p>
          <ul>
            {settings.kept.map((kept) => (
              <li key={kept.key + ":" + kept.reason} data-key={kept.key}>
                <code translate="no">{kept.key}</code> stays <span translate="no">{showValue(kept.value)}</span>.{" "}
                {KEPT_REASON[kept.reason] ?? kept.reason}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {settings.upgrade ? (
        <div className="tool-upgrade">
          <p>
            <strong>Saving also upgrades the settings file.</strong>
          </p>
          <p>{settings.upgrade.message}</p>
          {settings.backup ? (
            <p>
              {BACKUP_TEXT[settings.backup.state]} <Dir>{settings.backup.path}</Dir>
            </p>
          ) : null}
          {settings.backup?.state === "conflict" ? null : (
            <label className="tool-check" htmlFor={ACK_ID}>
              <input
                id={ACK_ID}
                type="checkbox"
                checked={review.acknowledged}
                onChange={(event) => acknowledge(event.target.checked)}
              />
              I understand: upgrade this project's settings file and save
            </label>
          )}
        </div>
      ) : null}
    </div>
  );
}

function PlanView({ review, acknowledge }: { review: Review; acknowledge: (value: boolean) => void }) {
  const { plan } = review;
  const copy = plan.action === "copy";
  return (
    <div className="tool-review">
      <PlanLead plan={plan} />
      {plan.folders.length > 0 ? (
        <p>
          Folders to create:{" "}
          {plan.folders.map((folder, index) => (
            <span key={folder}>
              {index > 0 ? ", " : ""}
              <Dir>{folder}</Dir>
            </span>
          ))}
        </p>
      ) : null}
      {plan.files.length > 0 ? (
        <div className="wiki-scroll" role="region" tabIndex={0} aria-label="Files of the plan">
          <table className="tool-plan">
            <caption>
              Files in <Dir>{plan.dir}</Dir>: {counted(plan.writes, "file is written", "files are written")}
            </caption>
            <thead>
              <tr>
                <th scope="col">File</th>
                <th scope="col">What happens</th>
                {copy ? <th scope="col">Size</th> : null}
              </tr>
            </thead>
            <tbody>
              {plan.files.map((file) => (
                <tr key={file.path} data-file={file.path} data-action={file.action}>
                  <th scope="row">
                    <code translate="no">{file.path}</code>
                  </th>
                  <td>{FILE_ACTION[file.action]}</td>
                  {copy ? <td className="wiki-number">{file.bytes === null ? "" : showBytes(file.bytes)}</td> : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {plan.blocked_total > 0 ? (
        <div className="wiki-block wiki-blocked">
          <p>
            <strong>
              {counted(plan.blocked_total, "thing stops", "things stop")} the copy. Fix them in <Dir>{plan.source ?? ""}</Dir>,
              then preview again.
            </strong>
          </p>
          <ul>
            {plan.blocked.map((blocked) => (
              <li key={blocked.path + ":" + blocked.reason + ":" + String(blocked.line)}>
                <code translate="no">
                  {blocked.path}
                  {blocked.line === null ? "" : ":" + String(blocked.line)}
                </code>{" "}
                {BLOCK_REASON[blocked.reason] ?? blocked.reason}
                {blocked.target ? (
                  <span>
                    {" "}
                    Link: <code translate="no">{blocked.target}</code>
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
          {plan.blocked_total > plan.blocked.length ? (
            <p className="muted">Only the first {WHOLE.format(plan.blocked.length)} are listed.</p>
          ) : null}
        </div>
      ) : null}
      {plan.left.length > 0 ? (
        <div className="wiki-block">
          <p>
            <strong>Not copied on purpose</strong>
          </p>
          <ul>
            {plan.left.map((path) => (
              <li key={path}>
                <code translate="no">{path}</code>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {plan.unresolved_total > 0 ? (
        <div className="wiki-block">
          <p>
            <strong>{counted(plan.unresolved_total, "link already leads", "links already lead")} nowhere.</strong> The copy
            does not change that.
          </p>
          <ul>
            {plan.unresolved_links.map((link) => (
              <li key={link.file + ":" + String(link.line) + ":" + link.target}>
                <code translate="no">
                  {link.file}:{link.line}
                </code>{" "}
                to <code translate="no">{link.target}</code>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <PlanSettings review={review} acknowledge={acknowledge} />
    </div>
  );
}

function settingsResultText(result: WikiApplyDto): string {
  const { settings } = result;
  switch (settings.state) {
    case "unchanged":
      return "The project's settings were not changed.";
    case "saved":
      return (
        "Saved the wiki folder to " +
        settings.config_path +
        "." +
        (settings.upgraded ? " The file was upgraded" + (settings.backup ? "; the original is kept as " + settings.backup + "." : ".") : "")
      );
    case "skipped":
      return "The project's settings were not saved, because not every file is in place.";
    case "stale":
      return "The project's settings were not saved: the settings file changed after the preview. The files stay.";
    case "failed":
      return "The project's settings were not saved. The files stay.";
  }
}

function ResultView({ result }: { result: WikiApplyDto }) {
  const { counts, stop, settings } = result;
  return (
    <div className="tool-review">
      <p>
        <strong>{result.complete ? "Done." : "Not finished."}</strong>{" "}
        {result.complete ? (
          <span>
            The wiki is in <Dir>{result.dir}</Dir>.
          </span>
        ) : (
          "Nothing was undone or replaced. Preview again to see what is left."
        )}
      </p>
      <p className="wiki-number">
        {counted(counts.created, "file created", "files created")}, {WHOLE.format(counts.kept)} kept
        {counts.failed > 0 ? ", " + WHOLE.format(counts.failed) + " failed" : ""}
        {counts.pending > 0 ? ", " + WHOLE.format(counts.pending) + " not started" : ""}.
      </p>
      {stop ? (
        <p className={stop.code === STOPPED ? undefined : "status-error"}>
          {stop.code === STOPPED ? (
            "Stopped at your request. The files already written stay; the rest were not started."
          ) : (
            <ErrorText error={stop} />
          )}
        </p>
      ) : null}
      {result.items.length > 0 ? (
        <div className="wiki-scroll" role="region" tabIndex={0} aria-label="Result for each file">
          <table className="tool-plan">
            <caption>
              Files in <Dir>{result.dir}</Dir>
            </caption>
            <thead>
              <tr>
                <th scope="col">File</th>
                <th scope="col">Result</th>
              </tr>
            </thead>
            <tbody>
              {result.items.map((item) => (
                <tr key={item.path} data-file={item.path} data-status={item.status}>
                  <th scope="row">
                    <code translate="no">{item.path}</code>
                  </th>
                  <td>{ITEM_STATUS[item.status]}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <p className={settings.error ? "status-error wiki-settings-result" : "wiki-settings-result"}>
        {settingsResultText(result)}
        {settings.error ? (
          <span>
            {" "}
            <ErrorText error={settings.error} />
          </span>
        ) : null}
      </p>
    </div>
  );
}

function SearchView({
  dir,
  search,
  onQuery,
  onSubmit,
}: {
  dir: string;
  search: SearchState;
  onQuery: (query: string) => void;
  onSubmit: () => void;
}) {
  const { result } = search;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onSubmit();
  };
  return (
    <div className="wiki-block wiki-search">
      <h5>Search the Wiki Text</h5>
      <form className="wiki-search-form" role="search" onSubmit={submit}>
        <label htmlFor={QUERY_ID}>
          Text to find in <Dir>{dir}</Dir>
        </label>
        <div className="wiki-search-row">
          <input
            id={QUERY_ID}
            type="search"
            name="wiki-query"
            autoComplete="off"
            value={search.query}
            aria-describedby={QUERY_ID + "-help"}
            onChange={(event) => onQuery(event.target.value)}
          />
          <button type="submit" aria-disabled={search.busy}>
            Search
          </button>
        </div>
        <span id={QUERY_ID + "-help"} className="muted">
          Looks through the Markdown files on this computer. Nothing is sent anywhere and no index is made.
        </span>
      </form>
      <p className="wiki-search-status" role="status">
        {search.busy
          ? "Searching…"
          : search.note
            ? search.note
            : result
              ? result.hits.length === 0
                ? "No match for “" + search.searched + "” in " + counted(result.files_searched, "file", "files") + "."
                : counted(result.hits.length, "match", "matches") +
                  " for “" +
                  search.searched +
                  "” in " +
                  counted(result.files_searched, "file", "files") +
                  "." +
                  (result.truncated ? " More exist; add a word to narrow the search." : "")
              : ""}
      </p>
      {search.error ? (
        <p className="status-error" role="alert">
          The search did not run. <ErrorText error={search.error} />
        </p>
      ) : null}
      {result && result.hits.length > 0 ? (
        <ul className="wiki-hits">
          {result.hits.map((hit) => (
            <li key={hit.file + ":" + String(hit.line)}>
              <span>
                <code translate="no">
                  {hit.file}:{hit.line}
                </code>
                {hit.heading ? <span className="muted"> under “{hit.heading}”</span> : null}
              </span>
              <span className="wiki-hit-context">{hit.context}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {result && result.skipped_total > 0 ? (
        <div className="wiki-block">
          <p className="muted">{counted(result.skipped_total, "entry was", "entries were")} not searched:</p>
          <ul className="muted">
            {result.skipped.map((skip) => (
              <li key={skip.path}>
                <code translate="no">{skip.path}</code> ({SKIP_REASON[skip.reason] ?? skip.reason})
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function Title({ phase }: { phase: Phase }) {
  if (phase.name === "overview") return <>Project Wiki Setup & Search</>;
  if (phase.name === "result") return <>{phase.result.complete ? "Wiki Result: Done" : "Wiki Result: Not Finished"}</>;
  const { plan } = phase.review;
  const lead = phase.name === "apply" ? "Now: " : "Review: ";
  const dir = <span translate="no">{plan.dir}</span>;
  if (plan.mode === "copy") {
    return (
      <>
        {lead}Copy <span translate="no">{plan.source ?? ""}</span> to {dir}
      </>
    );
  }
  return (
    <>
      {lead}
      {plan.mode === "create" ? "Create the Wiki in " : "Set Up the Wiki in "}
      {dir}
    </>
  );
}

function applyLabel(plan: WikiPlanDto): string {
  if (plan.writes === 0) return plan.settings?.upgrade ? "Upgrade and Save the Setting" : "Save the Wiki Setting";
  return (plan.action === "copy" ? "Copy " : "Create ") + counted(plan.writes, "File", "Files");
}

/**
 * The Project Wiki panel inside its card. Opening it reads the folders once; nothing is written until one previewed
 * plan is confirmed. Every step is numbered: an answer is used only while its step is still the newest, so a later
 * step, a closed panel and another project all make a late answer a no-op.
 */
export function ProjectWikiPanel({
  transport,
  projectId,
  onChanged,
  onClose,
}: {
  transport: Transport;
  projectId: string;
  /** Files or settings may have changed: the cards are checked again. */
  onChanged: () => void;
  onClose: () => void;
}) {
  const [state, setState] = useState<PanelState>(INITIAL);
  const [search, setSearch] = useState<SearchState>(NO_SEARCH);
  const step = useRef(0);
  const searchStep = useRef(0);
  const panel = useRef<HTMLElement>(null);
  const { overview, phase, busy, failure } = state;

  const settle = (ticket: number, update: (now: PanelState) => PanelState) => {
    if (ticket !== step.current) return;
    setState(update);
  };

  const readOverview = async () => {
    const ticket = ++step.current;
    setState((now) => ({ ...now, busy: "overview", phase: OVERVIEW, failure: null }));
    try {
      const answer = await transport.projectWikiOverview({ project_id: projectId, input: {} });
      if (answer.project_id !== projectId) throw wrongProject();
      settle(ticket, (now) => ({
        ...now,
        overview: answer,
        busy: null,
        choice: answer.candidates.some((candidate) => candidate.dir === now.choice) ? now.choice : null,
      }));
    } catch (raw) {
      settle(ticket, (now) => ({
        ...now,
        overview: null,
        busy: null,
        failure: {
          lead: "The wiki folders could not be checked. Nothing was changed.",
          error: toErrorDto(raw),
          retry: { kind: "overview" },
        },
      }));
    }
  };

  useEffect(() => {
    void readOverview();
    return () => {
      // A closed panel or another project: nothing in flight may land.
      step.current += 1;
      searchStep.current += 1;
    };
    // The panel is mounted for one project and one transport, and reads once when it opens.
  }, [transport, projectId]);

  // A step or a wait replaces the panel's contents and the button that was pressed, so focus goes to the panel.
  const phaseName = phase.name;
  const waiting = busy !== null;
  useEffect(() => {
    panel.current?.focus();
  }, [phaseName, waiting]);

  const preview = async (request: WikiActionReq) => {
    if (busy || phase.name === "apply") return;
    const ticket = ++step.current;
    setState((now) => ({ ...now, busy: "preview", phase: OVERVIEW, failure: null }));
    try {
      const plan = await transport.projectWikiPlan({ project_id: projectId, input: request });
      if (plan.project_id !== projectId) throw wrongProject();
      settle(ticket, (now) => ({ ...now, busy: null, phase: { name: "review", review: { request, plan, acknowledged: false } } }));
    } catch (raw) {
      settle(ticket, (now) => ({
        ...now,
        busy: null,
        failure: {
          lead: "The preview could not be made. Nothing was written.",
          error: toErrorDto(raw),
          retry: { kind: "preview", request },
        },
      }));
    }
  };

  const apply = async () => {
    if (busy || phase.name !== "review") return;
    const { review } = phase;
    if (gateOf(review.plan) !== "ready") return;
    if (review.plan.settings?.upgrade && !review.acknowledged) {
      setState((now) => ({ ...now, failure: { lead: "Tick the upgrade box to continue.", error: null, retry: null } }));
      return;
    }
    const input = wikiApplyRequestFor(review);
    const ticket = ++step.current;
    setState((now) => ({ ...now, failure: null, phase: { name: "apply", review, taskId: null, progress: null, stopping: false } }));
    const onEvent = (event: TaskEvent<WikiApplyDto>) => {
      if (ticket !== step.current) return;
      if (event.event === "progress") {
        setState((now) => (now.phase.name === "apply" ? { ...now, phase: { ...now.phase, progress: event.data } } : now));
        return;
      }
      // A terminal event ends this step: nothing else of it may land.
      step.current += 1;
      if (event.event === "done") {
        const result = event.data;
        if (result.project_id !== projectId || result.plan_id !== input.plan_id) {
          setState((now) => ({
            ...now,
            overview: null,
            phase: OVERVIEW,
            failure: {
              lead: "The answer did not match the reviewed plan, so the result is unknown.",
              error: { code: "UI_APPLY_RESPONSE_MISMATCH", message: "Look again to see what the project has now.", hint: null },
              retry: { kind: "overview" },
            },
          }));
        } else {
          setState((now) => ({ ...now, failure: null, phase: { name: "result", review, result } }));
        }
        onChanged();
      } else if (event.event === "failed") {
        const error = event.data;
        const stale = STALE_CODES.has(error.code);
        setState((now) =>
          stale
            ? {
                ...now,
                // A plan the project has moved on from is never offered again, and neither is the old overview.
                overview: null,
                phase: OVERVIEW,
                failure: {
                  lead: "Not applied: the project changed after the preview. Preview again.",
                  error,
                  retry: { kind: "preview", request: review.request },
                },
              }
            : { ...now, phase: { name: "review", review }, failure: { lead: "Not applied.", error, retry: null } },
        );
      } else {
        setState((now) => ({
          ...now,
          phase: { name: "review", review },
          failure: { lead: "Stopped before any file was written.", error: null, retry: null },
        }));
      }
    };
    try {
      const taskId = await transport.startProjectWikiApply({ project_id: projectId, input }, onEvent);
      settle(ticket, (now) => (now.phase.name === "apply" ? { ...now, phase: { ...now.phase, taskId } } : now));
    } catch (raw) {
      settle(ticket, (now) => ({
        ...now,
        phase: { name: "review", review },
        failure: { lead: "Not started. Nothing was written.", error: toErrorDto(raw), retry: null },
      }));
    }
  };

  const stop = async () => {
    if (phase.name !== "apply" || phase.taskId === null || phase.stopping) return;
    const ticket = step.current;
    const { taskId } = phase;
    setState((now) => (now.phase.name === "apply" ? { ...now, phase: { ...now.phase, stopping: true } } : now));
    try {
      await transport.taskCancel(taskId);
    } catch (raw) {
      settle(ticket, (now) =>
        now.phase.name === "apply"
          ? {
              ...now,
              phase: { ...now.phase, stopping: false },
              failure: { lead: "The stop request did not go through.", error: toErrorDto(raw), retry: null },
            }
          : now,
      );
    }
  };

  const acknowledge = (value: boolean) => {
    setState((now) =>
      now.phase.name === "review"
        ? { ...now, failure: null, phase: { name: "review", review: { ...now.phase.review, acknowledged: value } } }
        : now,
    );
  };

  const back = () => {
    if (busy || phase.name === "apply") return;
    if (phase.name === "result") {
      void readOverview();
      return;
    }
    step.current += 1;
    setState((now) => ({ ...now, phase: OVERVIEW, failure: null }));
  };

  const choose = (dir: string) => {
    if (busy) return;
    // A search result is for the folder it searched.
    searchStep.current += 1;
    setSearch((now) => ({ ...NO_SEARCH, query: now.query }));
    setState((now) => ({ ...now, choice: dir, failure: null }));
  };

  const retry = () => {
    if (!failure?.retry || busy) return;
    if (failure.retry.kind === "overview") void readOverview();
    else void preview(failure.retry.request);
  };

  const picked = overview ? pickedOf(overview, state.choice) : null;
  const found = overview !== null && picked !== null && overview.candidates.some((candidate) => candidate.dir === picked);
  const inUse = overview && (overview.target.state === "ready" || overview.target.state === "incomplete") ? overview.target.dir : null;
  const searchDir = found ? picked : inUse;

  const runSearch = async () => {
    if (search.busy || searchDir === null) return;
    const query = search.query.trim();
    if (query === "") {
      searchStep.current += 1;
      setSearch((now) => ({ ...NO_SEARCH, query: now.query, note: "Enter the text to find." }));
      document.getElementById(QUERY_ID)?.focus();
      return;
    }
    const ticket = ++searchStep.current;
    setSearch((now) => ({ ...now, busy: true, error: null, note: null }));
    try {
      const result = await transport.projectWikiSearch({
        project_id: projectId,
        // A folder that was found is named; otherwise core searches the folder in use.
        input: { dir: found ? picked : null, query, max_hits: null },
      });
      if (result.project_id !== projectId) throw wrongProject();
      if (ticket !== searchStep.current) return;
      setSearch((now) => ({ ...now, busy: false, searched: query, result }));
    } catch (raw) {
      if (ticket !== searchStep.current) return;
      setSearch((now) => ({ ...now, busy: false, result: null, error: toErrorDto(raw) }));
    }
  };

  const showOverview = phase.name === "overview" && overview !== null && busy === null;
  // Setup acts on the picked folder, else on the folder core would use when that is one it can set up.
  const setupDir = !overview
    ? null
    : found
      ? picked
      : overview.target.state === "none" || overview.target.state === "incomplete"
        ? overview.target.dir
        : null;
  const canSetup = showOverview && setupDir !== null && !(overview.target.state === "ready" && setupDir === overview.target.dir);
  const canCopy = showOverview && found && picked !== overview.default_dir;
  const gate = phase.name === "review" ? gateOf(phase.review.plan) : null;
  const needsAck = phase.name === "review" && phase.review.plan.settings?.upgrade ? !phase.review.acknowledged : false;
  const progress = phase.name === "apply" ? phase.progress : null;

  return (
    <section className="tool-settings wiki-panel" ref={panel} tabIndex={-1} aria-labelledby={TITLE_ID} data-phase={phase.name}>
      <h4 id={TITLE_ID}>
        <Title phase={phase} />
      </h4>
      {showOverview ? <OverviewView overview={overview} picked={picked} onChoose={choose} /> : null}
      {showOverview && (canSetup || canCopy) ? (
        <div className="tool-settings-actions">
          {canSetup ? (
            <button
              type="button"
              onClick={() => void preview({ action: "setup", selected: found ? picked : null, today: null })}
            >
              Preview Setup
            </button>
          ) : null}
          {canCopy && picked !== null ? (
            <button type="button" onClick={() => void preview({ action: "copy", source: picked })}>
              Preview Copy to <span translate="no">{overview.default_dir}</span>
            </button>
          ) : null}
        </div>
      ) : null}
      {showOverview && overview.target.state === "choose" && picked === null ? (
        <p className="muted">Choose a folder to set up, copy or search.</p>
      ) : null}
      {phase.name === "review" ? <PlanView review={phase.review} acknowledge={acknowledge} /> : null}
      {phase.name === "apply" ? (
        <div className="wiki-progress">
          {progress && progress.phase === "write-files" && progress.total !== null ? (
            <>
              <progress value={progress.done} max={Math.max(progress.total, 1)} aria-label="Files written" />
              <span className="wiki-number">
                {WHOLE.format(progress.done)} of {WHOLE.format(progress.total)}
              </span>
            </>
          ) : null}
        </div>
      ) : null}
      {phase.name === "result" ? <ResultView result={phase.result} /> : null}
      <div className="tool-settings-feedback" role="status">
        {busy ? (
          <p>{busy === "overview" ? "Looking for wiki folders… Nothing is changed." : "Working out what would change… Nothing is written yet."}</p>
        ) : phase.name === "apply" ? (
          <p>
            {phase.stopping
              ? "Stopping before the next file…"
              : (PROGRESS_TEXT[progress?.phase ?? "verify"] ?? "Working…")}
          </p>
        ) : null}
        {!busy && failure ? (
          <p className={failure.error ? "status-error" : undefined}>
            {failure.lead}
            {failure.error ? (
              <span>
                {" "}
                <ErrorText error={failure.error} />
              </span>
            ) : null}
          </p>
        ) : !busy && gate === "ready" ? (
          <p>Nothing is written yet. Files are written first; nothing that is there is replaced.</p>
        ) : !busy && gate === "nothing" ? (
          <p>Nothing to do: every file is in place and the settings already lead to this folder.</p>
        ) : !busy && gate === "blocked" ? (
          <p>This plan cannot be applied until what blocks it is fixed.</p>
        ) : !busy && gate === "backup-conflict" ? (
          <p>This plan cannot be applied until the file at the backup path is moved away.</p>
        ) : null}
        {!busy && failure?.retry ? (
          <button type="button" onClick={retry}>
            {failure.retry.kind === "overview" ? "Look Again" : "Preview Again"}
          </button>
        ) : null}
      </div>
      {showOverview && searchDir !== null ? (
        <SearchView
          dir={searchDir}
          search={search}
          onQuery={(query) => setSearch((now) => ({ ...now, query, note: null }))}
          onSubmit={() => void runSearch()}
        />
      ) : null}
      <div className="tool-settings-actions">
        {phase.name === "review" && gate === "ready" ? (
          <button type="button" className="tool-save" aria-disabled={needsAck} onClick={() => void apply()}>
            {applyLabel(phase.review.plan)}
          </button>
        ) : null}
        {phase.name === "apply" ? (
          <button type="button" aria-disabled={phase.taskId === null || phase.stopping} onClick={() => void stop()}>
            Stop Before the Next File
          </button>
        ) : null}
        {phase.name === "result" && !phase.result.complete ? (
          <button
            type="button"
            onClick={() => {
              const { request } = phase.review;
              void preview(request.action === "setup" ? { ...request, today: null } : request);
            }}
          >
            Preview Again
          </button>
        ) : null}
        {phase.name === "review" || phase.name === "result" ? (
          <button type="button" onClick={back}>
            Back to the Wiki Overview
          </button>
        ) : null}
        {showOverview ? (
          <button type="button" onClick={() => void readOverview()}>
            Look Again
          </button>
        ) : null}
        <button
          type="button"
          aria-disabled={phase.name === "apply"}
          onClick={() => {
            if (phase.name !== "apply") onClose();
          }}
        >
          Close
        </button>
      </div>
    </section>
  );
}
