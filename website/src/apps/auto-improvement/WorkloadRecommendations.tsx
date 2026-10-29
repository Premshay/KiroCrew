import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import ErrorNotice from "../../components/ErrorNotice";
import SimpleSelect from "../../components/SimpleSelect";
import { Btn, PanelSectionHeader } from "../../components/ui";
import type { TestEnvironment } from "./TestEnvironmentPanel";
import {
  workloadSettings,
  type MeasurementSettings,
} from "./measurementSettings";
import RepositoryInventory, {
  type RepositoryInventoryData,
} from "./RepositoryInventory";
import RepositoryAssessment from "./RepositoryAssessment";

interface RepositorySurvey extends RepositoryInventoryData {
  revision: string;
  declarationPath?: string | null;
  evidence: string[];
  warnings: string[];
  workloads: Array<{
    id: string;
    label: string;
    description: string;
    source: string;
    config: Record<string, unknown>;
  }>;
  environment: TestEnvironment | null;
}

export default function WorkloadRecommendations({
  repository,
  branch,
  locked,
  dirty,
  measurement,
  onWorkload,
  onEnvironment,
}: {
  repository: string;
  branch: string;
  locked: boolean;
  dirty: boolean;
  measurement: MeasurementSettings;
  onWorkload: (config: Record<string, unknown>) => void;
  onEnvironment: (environment: TestEnvironment) => void;
}) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState("");
  const survey = useQuery({
    queryKey: ["auto-improvement-repository-survey", repository, branch],
    queryFn: async () => {
      const response = await fetch(
        "/api/apps/auto-improvement/repository/survey",
      );
      const data = await response.json();
      if (!response.ok)
        throw new Error(
          data.error ||
            t("testEnvironment.failed", { status: response.status }),
        );
      return data as RepositorySurvey;
    },
    enabled: Boolean(repository),
  });
  const workloads = survey.data?.workloads ?? [];
  const workload = workloads.find((item) => item.id === selected);
  const preview = workload
    ? workloadSettings(measurement, workload.config)
    : null;
  const environment = survey.data?.environment;
  const display = (key: keyof MeasurementSettings, value: string) =>
    key === "track"
      ? t(`boundedMeasurement.${value}`)
      : key === "benchmarkResultMode"
        ? t(`boundedMeasurement.${value}`)
        : value || "∅";
  return (
    <section className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <PanelSectionHeader label={t("guidedWorkloads.title")} />
        <Btn
          disabled={locked || survey.isFetching}
          onClick={() => {
            void survey.refetch();
          }}
        >
          {t("guidedWorkloads.refresh")}
        </Btn>
      </div>
      <p className="text-[12px] text-muted">
        {t("guidedWorkloads.surveyHint")}
      </p>
      {survey.isFetching ? (
        <p role="status" className="text-[12px] text-muted">
          {t("guidedWorkloads.loading")}
        </p>
      ) : null}
      {/* No hand-off: the environment and workload forms may contain unsaved edits. */}
      <ErrorNotice message={survey.error?.message} />
      {(["bug", "perf"] as const).map((track) => {
        const matching = workloads.filter(
          (item) => (item.config.track ?? "bug") === track,
        );
        return (
          <label key={track} className="block space-y-1 text-[13px]">
            <span>{t(`boundedMeasurement.${track}`)}</span>
            <SimpleSelect
              options={["", ...matching.map((item) => item.id)]}
              optionLabels={[
                t("repositoryAssessment.choose"),
                ...matching.map((item) => item.label),
              ]}
              value={
                matching.some((item) => item.id === selected) ? selected : ""
              }
              onChange={setSelected}
              disabled={locked || dirty || !matching.length}
              aria-label={t(`boundedMeasurement.${track}`)}
            />
            {track === "perf" && !matching.length && survey.isSuccess ? (
              <p className="text-[12px] text-muted">
                {t("repositoryAssessment.noPerformance")}
              </p>
            ) : null}
          </label>
        );
      })}
      {workload && preview ? (
        <>
          <p className="text-[13px]">{workload.description}</p>
          <p className="text-[12px]">{t("repositoryAssessment.preview")}</p>
          <dl className="max-h-40 space-y-2 overflow-auto break-words text-[12px] select-text">
            {(Object.keys(preview) as Array<keyof MeasurementSettings>).map(
              (key) =>
                preview[key] !== measurement[key] ? (
                  <div key={key}>
                    <dt className="text-muted">
                      {t(
                        `${["editAllowlist", "scopeDiffBase", "suiteTimeoutSeconds", "maxCycles", "maxHours", "maxCostUsd", "calibrationReps", "measureReps"].includes(key) ? "guidedWorkloads" : "boundedMeasurement"}.${key === "benchmarkResultMode" ? "resultMode" : key}`,
                      )}
                    </dt>
                    <dd>
                      <code className="whitespace-pre-wrap">
                        {display(key, measurement[key])} →{" "}
                        {display(key, preview[key])}
                      </code>
                    </dd>
                  </div>
                ) : null,
            )}
          </dl>
          <Btn
            disabled={locked || dirty}
            onClick={() => onWorkload(workload.config)}
          >
            {t("repositoryAssessment.apply")}
          </Btn>
        </>
      ) : survey.isSuccess && !workloads.length ? (
        <p className="text-[12px] text-muted">{t("guidedWorkloads.empty")}</p>
      ) : null}
      {survey.data ? <RepositoryInventory data={survey.data} /> : null}
      <RepositoryAssessment
        key={survey.data?.revision}
        revision={survey.data?.revision ?? ""}
        repository={repository}
        branch={branch}
        locked={locked}
        areas={survey.data?.areas ?? []}
      />
      {dirty ? (
        <p className="text-[12px] text-muted">
          {t("guidedWorkloads.saveFirst")}
        </p>
      ) : null}
      {environment ? (
        <details className="text-[12px]">
          <summary className="cursor-pointer text-muted">
            {t("guidedWorkloads.environment")}
          </summary>
          <div className="mt-2 max-h-48 space-y-2 overflow-auto select-text">
            <p>{t("guidedWorkloads.environmentHint")}</p>
            <code className="block break-all">
              {environment.runnerPath ??
                environment.runnerExecutable ??
                environment.pythonExecutable ??
                environment.kind}
            </code>
            {Object.entries(environment.variables ?? {}).map(
              ([name, value]) => (
                <p key={name}>
                  <code>
                    {name}={value}
                  </code>
                </p>
              ),
            )}
            <Btn
              disabled={locked || dirty}
              onClick={() => onEnvironment(environment)}
            >
              {t("guidedWorkloads.applyEnvironment")}
            </Btn>
          </div>
        </details>
      ) : null}
      {survey.data ? (
        <details className="text-[12px]">
          <summary className="cursor-pointer text-muted">
            {t("guidedWorkloads.evidence")}
          </summary>
          <div className="mt-2 max-h-48 space-y-1 overflow-auto break-words select-text">
            <code>{survey.data.revision}</code>
            {survey.data.declarationPath ? (
              <p>{survey.data.declarationPath}</p>
            ) : null}
            {(survey.data.evidence ?? []).map((item, index) => (
              <p key={index}>{item}</p>
            ))}
          </div>
        </details>
      ) : null}
      {(survey.data?.warnings ?? []).map((warning, index) => (
        <p key={index} className="text-[12px] text-warn">
          {warning}
        </p>
      ))}
    </section>
  );
}
