// 한국어: 프로젝트 도구 설정 편집 — 저장된 값 읽기(빈 계획) → 양식 수정 → 미리보기 계획 → 확인 후 적용. 계획은 프로젝트 ID·수정 목록·계획 ID에 묶이고, 취소·프로젝트 변경·미리보기 뒤 수정은 옛 계획을 버린다.
import { useEffect, useRef, useState } from "react";
import type {
  ErrorDto,
  ProjectToolId,
  ProjectToolsApplyReq,
  ProjectToolsEdit,
  ProjectToolsPlanDto,
  ProjectToolsSettingsDto,
  ToolSettingValue,
  ToolsBackupDto,
} from "./contract";
import { toErrorDto, type Transport } from "./transport/types";

/** What the fields hold while the user edits. Lists are one entry per line. */
export interface ToolForm {
  wikiDir: string;
  indexManaged: boolean;
  indexExclude: string;
  indexModel: string;
  ownWikiDirs: boolean;
  wikiDirs: string;
  graphFile: string;
  graphifyManaged: boolean;
  jevEnabled: boolean;
  impactRerank: boolean;
}

export type ToolField = keyof ToolForm;

export function formOf(settings: ProjectToolsSettingsDto): ToolForm {
  const { wiki, impact, jev, managed } = settings;
  return {
    wikiDir: wiki.dir.value,
    indexManaged: managed.wiki_index.managed.value,
    indexExclude: managed.wiki_index.exclude.value.join("\n"),
    indexModel: managed.wiki_index.embedding_model.value,
    ownWikiDirs: impact.wiki_dirs.explicit,
    wikiDirs: impact.wiki_dirs.value.join("\n"),
    graphFile: impact.graph_file.value,
    graphifyManaged: managed.graphify.managed.value,
    jevEnabled: jev.enabled.value,
    impactRerank: impact.rerank.value,
  };
}

function lines(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");
}

function sameList(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((item, index) => item === b[index]);
}

/** Only what the user changed from the saved settings becomes an edit, in the order core lists the keys. */
export function editsFor(settings: ProjectToolsSettingsDto, form: ToolForm): ProjectToolsEdit[] {
  const { wiki, impact, jev, managed } = settings;
  const edits: ProjectToolsEdit[] = [];
  const wikiDir = form.wikiDir.trim();
  if (wikiDir !== wiki.dir.value) edits.push({ kind: "wiki-dir", value: wikiDir });
  if (!form.ownWikiDirs) {
    if (impact.wiki_dirs.explicit) edits.push({ kind: "impact-wiki-dirs", value: null });
  } else {
    const dirs = lines(form.wikiDirs);
    if (!impact.wiki_dirs.explicit || !sameList(dirs, impact.wiki_dirs.value)) {
      edits.push({ kind: "impact-wiki-dirs", value: dirs });
    }
  }
  const graphFile = form.graphFile.trim();
  if (graphFile !== impact.graph_file.value) edits.push({ kind: "impact-graph-file", value: graphFile });
  if (form.impactRerank !== impact.rerank.value) edits.push({ kind: "impact-rerank", value: form.impactRerank });
  if (form.jevEnabled !== jev.enabled.value) edits.push({ kind: "jev-enabled", value: form.jevEnabled });
  if (form.indexManaged !== managed.wiki_index.managed.value) {
    edits.push({ kind: "wiki-index-managed", value: form.indexManaged });
  }
  const exclude = lines(form.indexExclude);
  if (!sameList(exclude, managed.wiki_index.exclude.value)) edits.push({ kind: "wiki-index-exclude", value: exclude });
  const model = form.indexModel.trim();
  if (model !== managed.wiki_index.embedding_model.value) {
    edits.push({ kind: "wiki-index-embedding-model", value: model });
  }
  if (form.graphifyManaged !== managed.graphify.managed.value) {
    edits.push({ kind: "graphify-managed", value: form.graphifyManaged });
  }
  return edits;
}

/** Guidance beside a field before anything is asked of core; core's own checks still decide. */
function fieldProblems(form: ToolForm): Partial<Record<ToolField, string>> {
  const problems: Partial<Record<ToolField, string>> = {};
  if (form.wikiDir.trim() === "") problems.wikiDir = "Enter a folder inside the project, such as docs/wiki.";
  if (form.graphFile.trim() === "") problems.graphFile = "Enter a file inside the project, such as graphify-out/graph.json.";
  if (form.ownWikiDirs && lines(form.wikiDirs).length === 0) {
    problems.wikiDirs = "List at least one folder, or untick the box above.";
  }
  return problems;
}

/** A settings value as the file writes it; a key the file does not have is "not set". */
export function showValue(value: ToolSettingValue | null | undefined): string {
  if (value === null || value === undefined) return "not set";
  if (Array.isArray(value)) return "[" + value.map((item) => JSON.stringify(item)).join(", ") + "]";
  return JSON.stringify(value);
}

const KEY_LABEL: Record<string, string> = {
  "wiki.dir": "Wiki folder",
  "impact.wiki_dirs": "Wiki folders the Impact Map reads",
  "impact.graph_file": "Graph file the Impact Map reads",
  "impact.rerank": "Re-order Impact Map results with Jev",
  "rerank.enabled": "Use Jev in this project",
  "rerank.provider": "Jev provider",
  "project_tools.wiki_index.managed": "Ask for a local wiki index",
  "project_tools.wiki_index.exclude": "Left out of the wiki index",
  "project_tools.wiki_index.embedding_model": "Wiki index embedding model",
  "project_tools.graphify.managed": "Ask for a refreshed graph file",
};

const KEPT_REASON: Record<string, string> = {
  "explicit-wiki-dirs": "The Impact Map has its own list of wiki folders, so the new wiki folder does not replace it.",
  "other-provider": "The settings name another provider, so turning Jev on does not replace it.",
};

/** The codes that mean the reviewed plan no longer matches the project's file; a new preview is the only way on. */
const STALE_CODES = new Set(["OAH_PLAN_STALE", "OAH_CONFIG_CHANGED", "OAH_CONFIG_UPGRADE_REQUIRED"]);

type Step = "read" | "preview" | "save";

/** The edits the user previewed and the plan core answered with; a save is built from this alone. */
interface Review {
  readonly plan: ProjectToolsPlanDto;
  readonly edits: readonly ProjectToolsEdit[];
  /** The separate acknowledgement of this plan's upgrade; it never carries over to another plan. */
  readonly acknowledged: boolean;
}

interface Editor {
  readonly projectId: string;
  readonly tool: ProjectToolId;
  /** The saved settings, read once when the editor opened. */
  readonly snapshot: ProjectToolsPlanDto | null;
  readonly form: ToolForm | null;
  readonly problems: Partial<Record<ToolField, string>>;
  readonly busy: Step | null;
  readonly review: Review | null;
  /** The one message of the feedback region: why the last step failed and which step Try Again repeats. */
  readonly failure: { readonly lead: string; readonly error: ErrorDto | null; readonly retry: Step | null } | null;
}

export interface ToolSettings {
  /** The open editor of the current project, or `null`. */
  readonly editor: Editor | null;
  open(tool: ProjectToolId): void;
  change(patch: Partial<ToolForm>): void;
  preview(): void;
  editAgain(): void;
  acknowledge(value: boolean): void;
  save(): void;
  retry(): void;
  cancel(): void;
}

function wrongProject(): ErrorDto {
  return {
    code: "UI_PROJECT_CHANGED",
    message: "The answer was for another project, so it is not used.",
    hint: null,
  };
}

/** The native request, built from the reviewed plan alone: its edits, its ID and its acknowledged upgrade. */
export function applyRequestFor(review: Review): ProjectToolsApplyReq {
  const req: ProjectToolsApplyReq = { edits: [...review.edits], plan_id: review.plan.plan_id };
  if (review.plan.upgrade && review.acknowledged) req.approve_upgrade = review.plan.upgrade.fingerprint;
  return req;
}

function savedText(review: Review, result: { saved: boolean; upgraded: boolean; backup: string | null }): string {
  const file = review.plan.config_path;
  if (!result.saved) return "Nothing needed saving: " + file + " already had these settings.";
  if (!result.upgraded) return "Saved to " + file + ".";
  return "Upgraded and saved " + file + "." + (result.backup ? " The original is kept as " + result.backup + "." : "");
}

/**
 * One settings editor for the open project. Every step is numbered: an answer is used only while its step is
 * still the newest, so Cancel, another project and a later edit all make a late answer a no-op.
 */
export function useToolSettings(
  transport: Transport,
  projectId: string,
  onSaved: (tool: ProjectToolId, text: string) => void,
): ToolSettings {
  const [state, setState] = useState<Editor | null>(null);
  const step = useRef(0);

  useEffect(
    () => () => {
      // Another project or a closed page: nothing in flight may land and no editor is carried over.
      step.current += 1;
      setState(null);
    },
    [projectId],
  );

  // An editor made under another project is never shown or used, even for the one render before it is cleared.
  const editor = state?.projectId === projectId ? state : null;

  const settle = (ticket: number, update: (editor: Editor) => Editor | null) => {
    if (ticket !== step.current) return false;
    setState((previous) => (previous ? update(previous) : previous));
    return true;
  };

  const read = async (tool: ProjectToolId) => {
    const ticket = ++step.current;
    setState({ projectId, tool, snapshot: null, form: null, problems: {}, busy: "read", review: null, failure: null });
    try {
      const plan = await transport.projectToolsPlan({ project_id: projectId, input: { edits: [] } });
      if (plan.project_id !== projectId) throw wrongProject();
      settle(ticket, (now) => ({ ...now, snapshot: plan, form: formOf(plan.settings), busy: null }));
    } catch (raw) {
      settle(ticket, (now) => ({
        ...now,
        busy: null,
        failure: { lead: "The saved settings could not be read.", error: toErrorDto(raw), retry: "read" },
      }));
    }
  };

  const preview = async () => {
    if (!editor || editor.busy || !editor.snapshot || !editor.form) return;
    const problems = fieldProblems(editor.form);
    if (Object.keys(problems).length > 0) {
      setState({ ...editor, problems, review: null, failure: { lead: "Fix the marked setting first.", error: null, retry: null } });
      return;
    }
    const edits = editsFor(editor.snapshot.settings, editor.form);
    if (edits.length === 0) {
      setState({
        ...editor,
        problems,
        review: null,
        failure: { lead: "Nothing to preview: the form matches the saved settings.", error: null, retry: null },
      });
      return;
    }
    const ticket = ++step.current;
    setState({ ...editor, problems, busy: "preview", review: null, failure: null });
    try {
      const plan = await transport.projectToolsPlan({ project_id: projectId, input: { edits } });
      if (plan.project_id !== projectId) throw wrongProject();
      settle(ticket, (now) => ({ ...now, busy: null, review: { plan, edits, acknowledged: false } }));
    } catch (raw) {
      settle(ticket, (now) => ({
        ...now,
        busy: null,
        failure: { lead: "The changes could not be previewed. Nothing was saved.", error: toErrorDto(raw), retry: "preview" },
      }));
    }
  };

  const save = async () => {
    if (!editor || editor.busy || !editor.review) return;
    const { review, tool } = editor;
    if (review.plan.changes.length === 0 || review.plan.backup?.state === "conflict") return;
    if (review.plan.upgrade && !review.acknowledged) {
      setState({ ...editor, failure: { lead: "Tick the upgrade box to save.", error: null, retry: null } });
      return;
    }
    const input = applyRequestFor(review);
    const ticket = ++step.current;
    setState({ ...editor, busy: "save", failure: null });
    try {
      const result = await transport.projectToolsApply({ project_id: projectId, input });
      if (ticket !== step.current) return;
      if (result.project_id !== projectId || result.plan_id !== input.plan_id) {
        settle(ticket, (now) => ({
          ...now,
          busy: null,
          review: null,
          failure: {
            lead: "The answer did not match the reviewed plan, so the result is unknown.",
            error: { code: "UI_APPLY_RESPONSE_MISMATCH", message: "Preview again to see what the file has now.", hint: null },
            retry: "preview",
          },
        }));
        return;
      }
      step.current += 1;
      setState(null);
      onSaved(tool, savedText(review, result));
    } catch (raw) {
      const error = toErrorDto(raw);
      const stale = STALE_CODES.has(error.code);
      settle(ticket, (now) => ({
        ...now,
        busy: null,
        // A plan the file has moved on from is never offered for saving again.
        review: stale ? null : now.review,
        failure: stale
          ? {
              lead: "Not saved: the settings file changed after the preview. Preview the changes again.",
              error,
              retry: "preview",
            }
          : { lead: "Not saved.", error, retry: "save" },
      }));
    }
  };

  return {
    editor,
    open: (tool) => void read(tool),
    change(patch) {
      if (!editor?.form || editor.busy) return;
      // Whatever was previewed was for the old contents.
      step.current += 1;
      setState({ ...editor, form: { ...editor.form, ...patch }, problems: {}, review: null, failure: null });
    },
    preview: () => void preview(),
    editAgain() {
      if (!editor || editor.busy) return;
      step.current += 1;
      setState({ ...editor, review: null, failure: null });
    },
    acknowledge(value) {
      if (!editor?.review || editor.busy) return;
      setState({ ...editor, review: { ...editor.review, acknowledged: value }, failure: null });
    },
    save: () => void save(),
    retry() {
      if (!editor?.failure?.retry || editor.busy) return;
      if (editor.failure.retry === "read") void read(editor.tool);
      else if (editor.failure.retry === "preview") void preview();
      else void save();
    },
    cancel() {
      // A save that already left cannot be recalled; its answer is waited for.
      if (!editor || editor.busy === "save") return;
      step.current += 1;
      setState(null);
    },
  };
}

const BUSY_TEXT: Record<Step, string> = {
  read: "Reading the saved settings…",
  preview: "Working out the changes… Nothing is saved yet.",
  save: "Saving…",
};

const BACKUP_TEXT: Record<ToolsBackupDto["state"], string> = {
  write: "The file as it is now will be kept as",
  reuse: "A backup with the same contents already exists and is kept:",
  conflict: "Another file is already at the backup path. Move it away, then preview again:",
};

function TextField({
  id,
  label,
  help,
  value,
  problem,
  onChange,
}: {
  id: string;
  label: string;
  help: string;
  value: string;
  problem?: string;
  onChange: (value: string) => void;
}) {
  return (
    <div className="tool-field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        type="text"
        value={value}
        spellCheck={false}
        autoComplete="off"
        translate="no"
        aria-invalid={problem ? true : undefined}
        aria-describedby={problem ? id + "-help " + id + "-problem" : id + "-help"}
        onChange={(event) => onChange(event.target.value)}
      />
      <span id={id + "-help"} className="muted">
        {help}
      </span>
      {problem ? (
        <span id={id + "-problem"} className="field-problem">
          {problem}
        </span>
      ) : null}
    </div>
  );
}

function ListField({
  id,
  label,
  help,
  value,
  disabled,
  problem,
  onChange,
}: {
  id: string;
  label: string;
  help: string;
  value: string;
  disabled?: boolean;
  problem?: string;
  onChange: (value: string) => void;
}) {
  return (
    <div className="tool-field">
      <label htmlFor={id}>{label}</label>
      <textarea
        id={id}
        rows={3}
        value={value}
        disabled={disabled}
        spellCheck={false}
        translate="no"
        aria-invalid={problem ? true : undefined}
        aria-describedby={problem ? id + "-help " + id + "-problem" : id + "-help"}
        onChange={(event) => onChange(event.target.value)}
      />
      <span id={id + "-help"} className="muted">
        {help}
      </span>
      {problem ? (
        <span id={id + "-problem"} className="field-problem">
          {problem}
        </span>
      ) : null}
    </div>
  );
}

function CheckField({
  id,
  label,
  help,
  checked,
  onChange,
}: {
  id: string;
  label: string;
  help: string;
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <div className="tool-field">
      <label className="tool-check" htmlFor={id}>
        <input
          id={id}
          type="checkbox"
          checked={checked}
          aria-describedby={id + "-help"}
          onChange={(event) => onChange(event.target.checked)}
        />
        {label}
      </label>
      <span id={id + "-help"} className="muted">
        {help}
      </span>
    </div>
  );
}

/** A flag saved here only records what the project asks for; it permits nothing on any machine. */
const ASKS_ONLY =
  "This only records the request in the project's settings. Nothing runs and nothing is sent; each machine still approves that separately, and that step is not in this app yet.";

function Fields({ editor, change }: { editor: Editor & { form: ToolForm; snapshot: ProjectToolsPlanDto }; change: ToolSettings["change"] }) {
  const { tool, form, problems, snapshot } = editor;
  const id = (field: string) => "tool-setting-" + tool + "-" + field;
  const offered = (section: "wiki-index" | "graphify") => snapshot.optional.includes(section);
  switch (tool) {
    case "wiki":
      return (
        <>
          <TextField
            id={id("dir")}
            label="Wiki folder"
            help="A folder inside the project, written with /. While the Impact Map has no list of its own, it reads this folder too."
            value={form.wikiDir}
            problem={problems.wikiDir}
            onChange={(wikiDir) => change({ wikiDir })}
          />
          {offered("wiki-index") ? (
            <fieldset className="tool-optional">
              <legend>Local wiki index (optional)</legend>
              <CheckField
                id={id("index-managed")}
                label="Ask for a local index of this wiki"
                help={ASKS_ONLY}
                checked={form.indexManaged}
                onChange={(indexManaged) => change({ indexManaged })}
              />
              <ListField
                id={id("index-exclude")}
                label="Leave out of the index"
                help="One pattern per line, below the wiki folder, such as drafts/**."
                value={form.indexExclude}
                onChange={(indexExclude) => change({ indexExclude })}
              />
              <TextField
                id={id("index-model")}
                label="Embedding model"
                help="A model name only. Leave empty for the default of each machine's index."
                value={form.indexModel}
                onChange={(indexModel) => change({ indexModel })}
              />
            </fieldset>
          ) : null}
        </>
      );
    case "impact-map":
      return (
        <>
          <CheckField
            id={id("own-wiki-dirs")}
            label="Give the Impact Map its own list of wiki folders"
            help="Unticked, the Impact Map follows the Project Wiki folder."
            checked={form.ownWikiDirs}
            onChange={(ownWikiDirs) => change({ ownWikiDirs })}
          />
          <ListField
            id={id("wiki-dirs")}
            label="Wiki folders the Impact Map reads"
            help="One folder per line, inside the project, written with /."
            value={form.wikiDirs}
            disabled={!form.ownWikiDirs}
            problem={problems.wikiDirs}
            onChange={(wikiDirs) => change({ wikiDirs })}
          />
          <TextField
            id={id("graph-file")}
            label="Graph file"
            help="A file inside the project, written with /. Naming a file here does not create, verify or use it."
            value={form.graphFile}
            problem={problems.graphFile}
            onChange={(graphFile) => change({ graphFile })}
          />
          {offered("graphify") ? (
            <fieldset className="tool-optional">
              <legend>Graph refresh (optional)</legend>
              <CheckField
                id={id("graphify-managed")}
                label="Ask for the graph file to be refreshed"
                help={ASKS_ONLY}
                checked={form.graphifyManaged}
                onChange={(graphifyManaged) => change({ graphifyManaged })}
              />
            </fieldset>
          ) : null}
        </>
      );
    case "jev":
      return (
        <>
          <p className="tool-boundary">
            These are project settings only. Saving them sends nothing, looks for no key and gives no permission to use
            one. A key and the approval to send data stay on each machine, and that step is not in this app yet.
          </p>
          <CheckField
            id={id("enabled")}
            label="Use Jev in this project"
            help={
              snapshot.settings.jev.provider.value
                ? "The settings name the provider " + snapshot.settings.jev.provider.value + "."
                : "Turning this on also names the provider when the settings name none."
            }
            checked={form.jevEnabled}
            onChange={(jevEnabled) => change({ jevEnabled })}
          />
          <CheckField
            id={id("impact-rerank")}
            label="Re-order Impact Map results with Jev"
            help="A separate setting: it has no effect unless Jev is used in this project and approved on the machine."
            checked={form.impactRerank}
            onChange={(impactRerank) => change({ impactRerank })}
          />
        </>
      );
  }
}

function ReviewView({ review, acknowledge }: { review: Review; acknowledge: ToolSettings["acknowledge"] }) {
  const { plan } = review;
  const ackId = "tool-upgrade-ack";
  return (
    <div className="tool-review">
      <p>
        File: <code translate="no">{plan.config_path}</code> <span className="muted">(in the project)</span>
      </p>
      {plan.changes.length === 0 ? (
        <p>Nothing to save: the file already has these settings.</p>
      ) : (
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
            {plan.changes.map((change) => (
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
      )}
      {plan.kept.length > 0 ? (
        <div className="tool-kept">
          <p>
            <strong>Left as they are</strong>
          </p>
          <ul>
            {plan.kept.map((kept) => (
              <li key={kept.key + ":" + kept.reason} data-key={kept.key}>
                <code translate="no">{kept.key}</code> stays <span translate="no">{showValue(kept.value)}</span>.{" "}
                {KEPT_REASON[kept.reason] ?? kept.reason}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {plan.upgrade && plan.changes.length > 0 ? (
        <div className="tool-upgrade">
          <p>
            <strong>This save also upgrades the settings file.</strong>
          </p>
          <p>{plan.upgrade.message}</p>
          {plan.backup ? (
            <p>
              {BACKUP_TEXT[plan.backup.state]} <code translate="no">{plan.backup.path}</code>
            </p>
          ) : null}
          {plan.backup?.state === "conflict" ? null : (
            <label className="tool-check" htmlFor={ackId}>
              <input
                id={ackId}
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

/** The open editor inside its card: the form, then the review of one plan, with one feedback region by the buttons. */
export function ToolSettingsPanel({ name, settings }: { name: string; settings: ToolSettings }) {
  const { editor } = settings;
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    panel.current?.focus();
  }, []);
  if (!editor) return null;
  const { review, busy, failure, form, snapshot } = editor;
  const titleId = "tool-settings-title-" + editor.tool;
  const blocked = review ? review.plan.changes.length === 0 || review.plan.backup?.state === "conflict" : false;
  const needsAck = review?.plan.upgrade ? !review.acknowledged : false;
  return (
    <section className="tool-settings" ref={panel} tabIndex={-1} aria-labelledby={titleId} data-phase={review ? "review" : "edit"}>
      <h4 id={titleId}>{review ? "Review the changes to " + name : name + " settings"}</h4>
      {review ? (
        <ReviewView review={review} acknowledge={settings.acknowledge} />
      ) : form && snapshot ? (
        <fieldset className="tool-fields" disabled={busy !== null}>
          <legend className="visually-hidden">{name} settings</legend>
          <Fields editor={{ ...editor, form, snapshot }} change={settings.change} />
          {snapshot.notices.length > 0 ? (
            <ul className="tool-notes">
              {snapshot.notices.map((notice) => (
                <li key={notice.id}>
                  {notice.code}: {notice.message}
                  {notice.hint ? " " + notice.hint : ""}
                </li>
              ))}
            </ul>
          ) : null}
        </fieldset>
      ) : null}
      <div className="tool-settings-feedback" role="status">
        {busy ? (
          <p>{BUSY_TEXT[busy]}</p>
        ) : failure ? (
          <p className={failure.error ? "status-error" : undefined}>
            {failure.lead}
            {failure.error ? (
              <span>
                {" "}
                {failure.error.code}: {failure.error.message}
                {failure.error.hint ? " " + failure.error.hint : ""}
              </span>
            ) : null}
          </p>
        ) : review && review.plan.changes.length > 0 ? (
          <p>Nothing is saved yet.</p>
        ) : null}
        {!busy && failure?.retry ? (
          <button type="button" onClick={settings.retry}>
            {failure.retry === "read" ? "Read Again" : failure.retry === "preview" ? "Preview Again" : "Try Saving Again"}
          </button>
        ) : null}
      </div>
      <div className="tool-settings-actions">
        {review ? (
          <>
            {blocked ? null : (
              <button
                type="button"
                className="tool-save"
                aria-disabled={busy !== null || needsAck}
                onClick={settings.save}
              >
                {review.plan.upgrade ? "Upgrade and Save" : "Save Changes"}
              </button>
            )}
            <button type="button" aria-disabled={busy !== null} onClick={settings.editAgain}>
              Edit Again
            </button>
          </>
        ) : form ? (
          <button type="button" aria-disabled={busy !== null} onClick={settings.preview}>
            Preview Changes
          </button>
        ) : null}
        <button type="button" aria-disabled={busy === "save"} onClick={settings.cancel}>
          {review || form ? "Cancel" : "Close"}
        </button>
      </div>
    </section>
  );
}
