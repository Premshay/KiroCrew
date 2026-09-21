import { useEffect, useId, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import ErrorNotice from "../../components/ErrorNotice";
import SimpleSelect from "../../components/SimpleSelect";
import { Badge, Btn, Input, PanelSectionHeader } from "../../components/ui";
import { i18nT } from "../../i18n/t";

export interface TestEnvironment {
  kind: "gateway" | "python" | "runner";
  pythonExecutable?: string;
  runnerExecutable?: string;
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
  const [revision, setRevision] = useState(0);
  const [validation, setValidation] = useState<string | null>(null);
  if (measurementSource !== settingsSource) {
    setMeasurementSource(settingsSource);
    if (!dirty) setMeasurement(measurementSettings(config));
  }
  const measurementDirty = JSON.stringify(measurement) !== settingsSource;
  function editMeasurement(patch: Partial<typeof measurement>) {
    setMeasurement((current) => ({ ...current, ...patch }));
    setDirty(true);
    setRevision((r) => r + 1);
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
    setRevision((r) => r + 1);
    setValidation(null);
  }
  const context = JSON.stringify([scope, revision]);
  const check = useMutation({
    mutationFn: async ({
      environment,
    }: {
      environment: TestEnvironment;
      context: string;
    }) => {
      const result = await requestJson<CheckResult>("/environment/check", {
        testEnvironment: environment,
      });
      if (result.ok !== true)
        throw new Error(
          diagnosticText(result.diagnostic) || i18nT("testEnvironment.blocked"),
        );
      return result;
    },
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
      setRevision((r) => r + 1);
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
  const pending = save.isPending || check.isPending || calibrate.isPending;
  const locked = disabled || pending;
  useEffect(() => {
    onBusyChange(dirty || pending);
  }, [dirty, pending, onBusyChange]);

  function edit(patch: Partial<TestEnvironment>) {
    setDraft((value) => ({ ...value, ...patch }));
    setDirty(true);
    setRevision((r) => r + 1);
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
    const runnerExecutable = draft.runnerExecutable?.trim();
    if (
      draft.kind === "runner" &&
      (!runnerExecutable || !/^(\/|[A-Za-z]:[\\/]|\\\\)/.test(runnerExecutable))
    ) {
      setValidation(t("testEnvironment.runnerPath"));
      return null;
    }
    setValidation(null);
    return {
      ...draft,
      pythonExecutable:
        draft.kind === "gateway" ? undefined : pythonExecutable || "python",
      runnerExecutable: draft.kind === "runner" ? runnerExecutable : undefined,
      variables: values,
    };
  }

  const currentCheck = check.variables?.context === context;
  const status =
    validation || save.isError
      ? "blocked"
      : currentCheck
        ? check.isPending
          ? "checking"
          : check.isError
            ? "blocked"
            : check.isSuccess
              ? "ready"
              : "unchecked"
        : "unchecked";
  const diagnostic =
    calibrate.error?.message ||
    save.error?.message ||
    (currentCheck ? check.error?.message : null);

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
                value={draft.runnerExecutable ?? ""}
                onChange={(e) => edit({ runnerExecutable: e.target.value })}
                disabled={locked}
                className="mt-1 w-full"
              />
            </label>
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
          {t(`boundedMeasurement.${key}`)}
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
          {t("testEnvironment.save")}
        </Btn>
        <Btn
          disabled={locked || measurementDirty}
          onClick={() => {
            const value = environment();
            if (value) check.mutate({ environment: value, context });
          }}
        >
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
            {t(`testEnvironment.${status}`)}
          </Badge>
        </span>
      </div>
      <p className="text-[12px] text-muted">
        {t(dirty ? "testEnvironment.unsaved" : "testEnvironment.saved")}
      </p>
      <div className="space-y-2">
        <Btn
          disabled={locked || dirty || status !== "ready"}
          aria-describedby={calibrationHintId}
          onClick={() => calibrate.mutate()}
        >
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
