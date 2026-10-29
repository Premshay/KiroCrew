import { useEffect, useId, useState } from "react";
import { Loader2 } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import ErrorNotice from "../../components/ErrorNotice";
import SimpleSelect from "../../components/SimpleSelect";
import { Badge, Btn, Input, PanelSectionHeader } from "../../components/ui";
import { i18nT } from "../../i18n/t";

const STATUS_LABEL_KEYS = {
  blocked: "testEnvironment.blocked",
  checking: "testEnvironment.checking",
  ready: "testEnvironment.ready",
  unchecked: "testEnvironment.unchecked",
} as const;
const BENCHMARK_LABEL_KEYS = {
  benchmarkCommand: "boundedMeasurement.benchmarkCommand",
  benchmarkCanaryCommand: "boundedMeasurement.benchmarkCanaryCommand",
} as const;

export interface TestEnvironment {
  kind: "gateway" | "python" | "runner";
  pythonExecutable?: string;
  runnerExecutable?: string;
  runnerPath?: string;
  runnerCommit?: string;
  variables?: Record<string, string>;
}

function measurementSettings(config: Record<string, unknown>) {
  return {
    track: String(config.track ?? "bug"),
    benchmarkCommand: String(config.benchmarkCommand ?? ""),
    benchmarkCanaryCommand: String(config.benchmarkCanaryCommand ?? ""),
    benchmarkResultMode: String(config.benchmarkResultMode ?? "wall"),
    benchmarkProtectedPaths: (
      (config.benchmarkProtectedPaths as string[] | undefined) ?? []
    ).join("\n"),
    focusedTestPaths: (
      (config.focusedTestPaths as string[] | undefined) ?? []
    ).join("\n"),
    fullRegressionTimeoutSeconds: String(
      config.fullRegressionTimeoutSeconds ?? 900,
    ),
  };
}

interface CheckResult {
  ok: boolean;
  diagnostic?: string | { stdout?: string; stderr?: string };
  tests_collected?: number;
}

/** The server's record of the last environment check, which outlives this page. */
interface CheckRecord {
  state:
    "none" | "checking" | "ready" | "blocked" | "interrupted" | "cancelled";
  fingerprint?: string;
  result?: CheckResult;
}

const CHECK_KEY = ["auto-improvement-environment-check"];

function diagnosticText(value: unknown): string | undefined {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return undefined;
  const diagnostic = value as { stderr?: unknown; stdout?: unknown };
  return (
    [diagnostic.stderr, diagnostic.stdout]
      .filter(
        (text): text is string => typeof text === "string" && Boolean(text),
      )
      .join("\n") || undefined
  );
}

const ABSOLUTE_PATH = /^(\/|[A-Za-z]:[\\/]|\\\\)/;

/** An absolute path names an installed runner; anything else is a path in the repository. */
function runnerLocation(value: string): Partial<TestEnvironment> {
  return ABSOLUTE_PATH.test(value.trim())
    ? {
        runnerExecutable: value,
        runnerPath: undefined,
        runnerCommit: undefined,
      }
    : {
        runnerPath: value,
        runnerExecutable: undefined,
        runnerCommit: undefined,
      };
}

function runnerSource(draft: TestEnvironment): Partial<TestEnvironment> | null {
  const executable = draft.runnerExecutable?.trim();
  if (executable)
    return ABSOLUTE_PATH.test(executable)
      ? { runnerExecutable: executable }
      : null;
  const path = draft.runnerPath?.trim();
  if (
    !path ||
    path.includes("\\") ||
    path.split("/").some((part) => ["", ".", ".."].includes(part))
  )
    return null;
  return { runnerPath: path, runnerCommit: draft.runnerCommit };
}

export async function requestJson<T>(
  path: string,
  body: unknown,
  method = "POST",
): Promise<T> {
  const response = await fetch(`/api/apps/auto-improvement${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = (await response.json()) as T & {
    error?: string;
    diagnostic?: CheckResult["diagnostic"];
    ok?: boolean;
  };
  if (!response.ok || (method !== "GET" && data.error) || data.ok === false) {
    throw new Error(
      data.error ||
        diagnosticText(data.diagnostic) ||
        i18nT("testEnvironment.failed", { status: response.status }),
    );
  }
  return data;
}

export default function TestEnvironmentPanel({
  config,
  branch,
  disabled,
  onBusyChange,
}: {
  config: Record<string, unknown>;
  branch: string;
  disabled: boolean;
  onBusyChange: (busy: boolean) => void;
}) {
  const { t } = useTranslation();
  const calibrationHintId = useId();
  const qc = useQueryClient();
  const settingsSource = JSON.stringify(measurementSettings(config));
  const [measurementSource, setMeasurementSource] = useState(settingsSource);
  const [measurement, setMeasurement] = useState(() =>
    measurementSettings(config),
  );
  const persisted = JSON.stringify(
    config.testEnvironment ?? { kind: "gateway" },
  );
  const [source, setSource] = useState(persisted);
  const [draft, setDraft] = useState<TestEnvironment>(() =>
    JSON.parse(persisted),
  );
  const [variables, setVariables] = useState(() =>
    Object.entries(draft.variables ?? {})
      .map(([k, v]) => `${k}=${v}`)
      .join("\n"),
  );
  const [dirty, setDirty] = useState(false);
  const [validation, setValidation] = useState<string | null>(null);
  if (measurementSource !== settingsSource) {
    setMeasurementSource(settingsSource);
    if (!dirty) setMeasurement(measurementSettings(config));
  }
  const measurementDirty = JSON.stringify(measurement) !== settingsSource;
  function editMeasurement(patch: Partial<typeof measurement>) {
    setMeasurement((current) => ({ ...current, ...patch }));
    setDirty(true);
  }
  if (source !== persisted) {
    setSource(persisted);
    if (!dirty) {
      const next = JSON.parse(persisted) as TestEnvironment;
      setDraft(next);
      setVariables(
        Object.entries(next.variables ?? {})
          .map(([k, v]) => `${k}=${v}`)
          .join("\n"),
      );
    }
  }

  const scope = JSON.stringify([config, branch, disabled]);
  const [previousScope, setPreviousScope] = useState(scope);
  if (previousScope !== scope) {
    setPreviousScope(scope);
    setValidation(null);
  }
  // What a check result applies to: the repository, the branch, the selection with its
  // variables and the saved measurement settings the server reads. Anything else is stale.
  const fingerprint = JSON.stringify([
    String(config.clone ?? ""),
    branch,
    draft,
    variables,
    settingsSource,
  ]);
  const checkRecord = useQuery({
    queryKey: CHECK_KEY,
    queryFn: () =>
      requestJson<CheckRecord>("/environment/check", undefined, "GET"),
    refetchInterval: (query) =>
      query.state.data?.state === "checking" ? 2000 : false,
  });
  const record = checkRecord.data;
  // A run clears the server's record when it starts; read it again around a run.
  useEffect(() => {
    qc.invalidateQueries({ queryKey: CHECK_KEY });
  }, [disabled, qc]);
  const serverChecking = record?.state === "checking";
  const check = useMutation({
    mutationFn: ({
      environment,
      fingerprint,
    }: {
      environment: TestEnvironment;
      fingerprint: string;
    }) =>
      requestJson<CheckRecord>("/environment/check", {
        testEnvironment: environment,
        fingerprint,
      }),
    onSuccess: (started) => qc.setQueryData(CHECK_KEY, started),
  });
  const save = useMutation({
    mutationFn: (environment: TestEnvironment) =>
      requestJson(
        "/config",
        {
          testEnvironment: environment,
          ...measurement,
          benchmarkProtectedPaths: measurement.benchmarkProtectedPaths
            .split("\n")
            .map((path) => path.trim())
            .filter(Boolean),
          focusedTestPaths: measurement.focusedTestPaths
            .split("\n")
            .map((path) => path.trim())
            .filter(Boolean),
          fullRegressionTimeoutSeconds: Number(
            measurement.fullRegressionTimeoutSeconds,
          ),
        },
        "PUT",
      ),
    onSuccess: () => {
      setDirty(false);
      qc.invalidateQueries({ queryKey: ["auto-improvement-config"] });
      qc.invalidateQueries({ queryKey: ["auto-improvement-ruler"] });
    },
  });
  const calibrate = useMutation({
    mutationFn: () =>
      requestJson<{ status: string; run_id: string }>("/calibrate", {}),
    onSuccess: (run) => {
      qc.setQueryData(["auto-improvement-run"], run);
      qc.invalidateQueries({ queryKey: ["auto-improvement-run"] });
      qc.invalidateQueries({ queryKey: ["auto-improvement-ruler"] });
    },
  });
  const checking = check.isPending || serverChecking;
  const pending = save.isPending || checking || calibrate.isPending;
  const locked = disabled || pending;
  useEffect(() => {
    onBusyChange(dirty || pending);
  }, [dirty, pending, onBusyChange]);

  function edit(patch: Partial<TestEnvironment>) {
    setDraft((value) => ({ ...value, ...patch }));
    setDirty(true);
    setValidation(null);
    save.reset();
  }

  function environment(): TestEnvironment | null {
    const values: Record<string, string> = {};
    for (const line of variables.split("\n").filter((line) => line.trim())) {
      const separator = line.indexOf("=");
      const name = line.slice(0, separator).trim();
      if (
        separator < 1 ||
        !/^[A-Za-z_][A-Za-z0-9_]*$/.test(name) ||
        Object.prototype.hasOwnProperty.call(values, name)
      ) {
        setValidation(t("testEnvironment.variablesInvalid"));
        return null;
      }
      Object.defineProperty(values, name, {
        value: line.slice(separator + 1),
        enumerable: true,
      });
    }
    const pythonExecutable = draft.pythonExecutable?.trim();
    if (
      draft.kind === "python" &&
      (!pythonExecutable || !/^(\/|[A-Za-z]:[\\/]|\\\\)/.test(pythonExecutable))
    ) {
      setValidation(t("testEnvironment.absolutePath"));
      return null;
    }
    const runner = runnerSource(draft);
    if (draft.kind === "runner" && !runner) {
      setValidation(t("testEnvironment.runnerPath"));
      return null;
    }
    setValidation(null);
    return {
      ...draft,
      pythonExecutable:
        draft.kind === "gateway" ? undefined : pythonExecutable || "python",
      runnerExecutable: undefined,
      runnerPath: undefined,
      runnerCommit: undefined,
      ...(draft.kind === "runner" ? runner : {}),
      variables: values,
    };
  }

  const refused = check.isError && check.variables?.fingerprint === fingerprint;
  const matching = record?.fingerprint === fingerprint;
  const status =
    validation || save.isError || refused
      ? "blocked"
      : checking && (check.isPending || matching)
        ? "checking"
        : matching && record?.state === "ready"
          ? "ready"
          : matching && record && record.state !== "none"
            ? "blocked"
            : "unchecked";
  const checkDiagnostic = !matching
    ? null
    : record?.state === "blocked"
      ? diagnosticText(record.result?.diagnostic) ||
        t("testEnvironment.blocked")
      : record?.state === "interrupted"
        ? t("testEnvironment.interrupted")
        : null;
  const diagnostic =
    calibrate.error?.message ||
    save.error?.message ||
    (refused ? check.error?.message : null) ||
    checkDiagnostic;

  return (
    <section className="mt-4 min-w-0 space-y-3">
      <PanelSectionHeader label={t("testEnvironment.title")} />
      <p className="text-[12px] text-muted">{t("testEnvironment.isolation")}</p>
      <SimpleSelect
        options={["gateway", "python", "runner"]}
        optionLabels={[
          t("testEnvironment.gateway"),
          t("testEnvironment.python"),
          t("testEnvironment.runner"),
        ]}
        value={draft.kind}
        onChange={(kind) =>
          edit({
            kind: kind as TestEnvironment["kind"],
            pythonExecutable: undefined,
            runnerExecutable: undefined,
            runnerPath: undefined,
            runnerCommit: undefined,
          })
        }
        disabled={locked}
        aria-label={t("testEnvironment.title")}
      />
      {draft.kind !== "gateway" ? (
        <>
          {draft.kind === "runner" ? (
            <label className="block text-[13px]">
              {t("testEnvironment.runnerExecutable")}
              <Input
                value={draft.runnerPath ?? draft.runnerExecutable ?? ""}
                onChange={(e) => edit(runnerLocation(e.target.value))}
                disabled={locked}
                className="mt-1 w-full"
              />
            </label>
          ) : null}
          {draft.kind === "runner" && draft.runnerPath !== undefined ? (
            <div className="flex flex-wrap items-center gap-2 text-[12px] text-muted">
              <span>
                {draft.runnerCommit
                  ? t("testEnvironment.runnerPinned", {
                      commit: draft.runnerCommit.slice(0, 12),
                    })
                  : t("testEnvironment.runnerUnpinned", { branch })}
              </span>
              {draft.runnerCommit && !dirty ? (
                <Btn
                  disabled={locked}
                  onClick={() => {
                    const value = environment();
                    if (value)
                      save.mutate({ ...value, runnerCommit: undefined });
                  }}
                >
                  {t("testEnvironment.runnerUpdate", { branch })}
                </Btn>
              ) : null}
            </div>
          ) : null}
          <label className="block text-[13px]">
            {t(
              draft.kind === "python"
                ? "testEnvironment.executable"
                : "testEnvironment.runnerPython",
            )}
            <Input
              value={draft.pythonExecutable ?? ""}
              onChange={(e) => edit({ pythonExecutable: e.target.value })}
              disabled={locked}
              className="mt-1 w-full"
            />
          </label>
          {draft.kind === "runner" ? (
            <p className="text-[12px] text-muted">
              {t("testEnvironment.runnerHint")}
            </p>
          ) : null}
        </>
      ) : null}
      <label className="block text-[13px]">
        {t("testEnvironment.variables")}
        <textarea
          aria-label={t("testEnvironment.variables")}
          value={variables}
          onChange={(e) => {
            setVariables(e.target.value);
            edit({});
          }}
          disabled={locked}
          rows={3}
          className="mt-1 w-full rounded border border-border bg-bg-elevated p-2 font-mono text-[13px] text-text"
        />
      </label>
      <PanelSectionHeader label={t("boundedMeasurement.title")} />
      <SimpleSelect
        options={["bug", "perf"]}
        optionLabels={[
          t("boundedMeasurement.bug"),
          t("boundedMeasurement.perf"),
        ]}
        value={measurement.track}
        onChange={(track) => editMeasurement({ track })}
        disabled={locked}
        aria-label={t("boundedMeasurement.track")}
      />
      {(["benchmarkCommand", "benchmarkCanaryCommand"] as const).map((key) => (
        <label key={key} className="block text-[13px]">
          {t(BENCHMARK_LABEL_KEYS[key])}
          <Input
            value={measurement[key]}
            onChange={(e) => editMeasurement({ [key]: e.target.value })}
            disabled={locked}
            className="mt-1 w-full"
          />
        </label>
      ))}
      <SimpleSelect
        options={["wall", "structured"]}
        optionLabels={[
          t("boundedMeasurement.wall"),
          t("boundedMeasurement.structured"),
        ]}
        value={measurement.benchmarkResultMode}
        onChange={(benchmarkResultMode) =>
          editMeasurement({ benchmarkResultMode })
        }
        disabled={locked}
        aria-label={t("boundedMeasurement.resultMode")}
      />
      <label className="block text-[13px]">
        {t("boundedMeasurement.benchmarkProtectedPaths")}
        <textarea
          aria-label={t("boundedMeasurement.benchmarkProtectedPaths")}
          value={measurement.benchmarkProtectedPaths}
          onChange={(e) =>
            editMeasurement({ benchmarkProtectedPaths: e.target.value })
          }
          disabled={locked}
          rows={3}
          className="mt-1 w-full rounded border border-border bg-bg-elevated p-2 font-mono text-[13px] text-text"
        />
      </label>
      <label className="block text-[13px]">
        {t("boundedMeasurement.focusedTestPaths")}
        <textarea
          aria-label={t("boundedMeasurement.focusedTestPaths")}
          value={measurement.focusedTestPaths}
          onChange={(e) =>
            editMeasurement({ focusedTestPaths: e.target.value })
          }
          disabled={locked}
          rows={3}
          className="mt-1 w-full rounded border border-border bg-bg-elevated p-2 font-mono text-[13px] text-text"
        />
      </label>
      <label className="block text-[13px]">
        {t("boundedMeasurement.fullRegressionTimeoutSeconds")}
        <Input
          type="number"
          min="1"
          value={measurement.fullRegressionTimeoutSeconds}
          onChange={(e) =>
            editMeasurement({ fullRegressionTimeoutSeconds: e.target.value })
          }
          disabled={locked}
          className="mt-1 w-full"
        />
      </label>
      <p className="text-[12px] text-muted">{t("boundedMeasurement.hint")}</p>
      <div className="flex flex-wrap items-center gap-2">
        <Btn
          disabled={locked || !dirty}
          onClick={() => {
            const value = environment();
            if (value) save.mutate(value);
          }}
        >
          {save.isPending ? (
            <Loader2
              className="lucide-inline animate-spin"
              aria-hidden="true"
            />
          ) : null}
          {t("testEnvironment.save")}
        </Btn>
        <Btn
          disabled={locked || measurementDirty}
          onClick={() => {
            const value = environment();
            if (value) check.mutate({ environment: value, fingerprint });
          }}
        >
          {checking ? (
            <Loader2
              className="lucide-inline animate-spin"
              aria-hidden="true"
            />
          ) : null}
          {t("testEnvironment.check")}
        </Btn>
        <span role="status" aria-live="polite">
          <Badge
            variant={
              status === "ready"
                ? "ok"
                : status === "blocked"
                  ? "warn"
                  : "muted"
            }
          >
            {t(STATUS_LABEL_KEYS[status])}
          </Badge>
        </span>
        {status === "ready" &&
        typeof record?.result?.tests_collected === "number" ? (
          <span className="text-[12px] text-muted">
            {t("testEnvironment.collected", {
              count: record.result.tests_collected,
            })}
          </span>
        ) : null}
      </div>
      {checking ? (
        <p className="text-[12px] text-muted">
          {t("testEnvironment.background")}
        </p>
      ) : null}
      <p className="text-[12px] text-muted">
        {t(dirty ? "testEnvironment.unsaved" : "testEnvironment.saved")}
      </p>
      <div className="space-y-2">
        <Btn
          disabled={locked || dirty || status !== "ready"}
          aria-describedby={calibrationHintId}
          onClick={() => calibrate.mutate()}
        >
          {calibrate.isPending ? (
            <Loader2
              className="lucide-inline animate-spin"
              aria-hidden="true"
            />
          ) : null}
          {t("autoImprovementCalibration.action")}
        </Btn>
        <p id={calibrationHintId} className="text-[12px] text-muted">
          {t("autoImprovementCalibration.warning")}
        </p>
      </div>
      <p role="status" aria-live="polite" className="text-[13px] text-muted">
        {validation}
      </p>
      {/* No hand-off: navigating away would discard the test environment draft. */}
      <ErrorNotice
        message={diagnostic}
        messageClassName="max-h-40 overflow-auto whitespace-pre-wrap break-words select-text"
      />
    </section>
  );
}
