/** Editable settings are whitelisted here; repository suggestions cannot alter publication policy. */
export const NUMBER_SETTINGS = {
  suiteTimeoutSeconds: { initial: 900, min: 1, max: 86400, integer: true },
  fullRegressionTimeoutSeconds: {
    initial: 900,
    min: 1,
    max: 86400,
    integer: true,
  },
  maxCycles: { initial: 25, min: 1, max: 1000, integer: true },
  maxHours: { initial: 2, min: 0.01, max: 168, integer: false },
  maxCostUsd: { initial: 5, min: 0.01, max: 100000, integer: false },
  calibrationReps: { initial: 5, min: 2, max: 10, integer: true },
  measureReps: { initial: "", min: 2, max: 100, integer: true },
} as const;

export function measurementSettings(config: Record<string, unknown>) {
  const lines = (key: string) =>
    Array.isArray(config[key]) ? (config[key] as string[]).join("\n") : "";
  return {
    track: String(config.track ?? "bug"),
    benchmarkCommand: String(config.benchmarkCommand ?? ""),
    benchmarkCanaryCommand: String(config.benchmarkCanaryCommand ?? ""),
    benchmarkResultMode: String(config.benchmarkResultMode ?? "wall"),
    benchmarkProtectedPaths: lines("benchmarkProtectedPaths"),
    focusedTestPaths: lines("focusedTestPaths"),
    editAllowlist: lines("editAllowlist"),
    scopeDiffBase: String(config.scopeDiffBase ?? ""),
    ...(Object.fromEntries(
      Object.entries(NUMBER_SETTINGS).map(([key, rule]) => [
        key,
        String(config[key] ?? rule.initial),
      ]),
    ) as Record<keyof typeof NUMBER_SETTINGS, string>),
  };
}

export type MeasurementSettings = ReturnType<typeof measurementSettings>;

export function settingsPayload(settings: MeasurementSettings) {
  return {
    ...settings,
    ...Object.fromEntries(
      ["benchmarkProtectedPaths", "focusedTestPaths", "editAllowlist"].map(
        (key) => [
          key,
          settings[key as keyof MeasurementSettings]
            .split("\n")
            .map((line) => line.trim())
            .filter(Boolean),
        ],
      ),
    ),
    ...Object.fromEntries(
      Object.keys(NUMBER_SETTINGS).map((key) => [
        key,
        key === "measureReps" && !settings.measureReps.trim()
          ? null
          : Number(settings[key as keyof MeasurementSettings]),
      ]),
    ),
  };
}

export function invalidNumber(
  settings: MeasurementSettings,
): keyof typeof NUMBER_SETTINGS | undefined {
  return (
    Object.keys(NUMBER_SETTINGS) as Array<keyof typeof NUMBER_SETTINGS>
  ).find((key) => {
    if (key === "measureReps" && !settings.measureReps.trim()) return false;
    const value = Number(settings[key]);
    const rule = NUMBER_SETTINGS[key];
    return (
      !settings[key].trim() ||
      !Number.isFinite(value) ||
      value < rule.min ||
      value > rule.max ||
      (rule.integer && !Number.isInteger(value))
    );
  });
}

export function workloadSettings(
  current: MeasurementSettings,
  suggestion: Record<string, unknown>,
): MeasurementSettings {
  // Reset workload-specific settings, while retaining the operator's run budget.
  const next = measurementSettings({
    ...settingsPayload(current),
    ...suggestion,
    track: suggestion.track ?? "bug",
    scopeDiffBase: suggestion.scopeDiffBase ?? "",
    editAllowlist: suggestion.editAllowlist ?? [],
    focusedTestPaths: suggestion.focusedTestPaths ?? [],
    benchmarkCommand: suggestion.benchmarkCommand ?? "",
    benchmarkCanaryCommand: suggestion.benchmarkCanaryCommand ?? "",
    benchmarkProtectedPaths: suggestion.benchmarkProtectedPaths ?? [],
    benchmarkResultMode: suggestion.benchmarkResultMode ?? "wall",
  });
  if (next.track === "bug") {
    next.benchmarkCommand = "";
    next.benchmarkCanaryCommand = "";
    next.benchmarkProtectedPaths = "";
    next.benchmarkResultMode = "wall";
  }
  return next;
}
