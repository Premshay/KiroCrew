import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Loader2 } from "lucide-react";
import { useState } from "react";
import SimpleSelect from "../../components/SimpleSelect";
import { Btn } from "../../components/ui";
import ErrorNotice from "../../components/ErrorNotice";

interface Assessment {
  state: string;
  error?: string;
  progress?: string;
  revision?: string;
  result?: {
    partial?: boolean;
    summary: string;
    findings: Array<{
      kind: string;
      title: string;
      detail: string;
      sourcePaths: string[];
      testPaths: string[];
      evidencePaths: string[];
      command?: string;
    }>;
  };
}

/** Agent findings are proposals to review, never executable workload settings. */
export default function RepositoryAssessment({
  repository,
  branch,
  locked,
  areas,
  revision,
}: {
  repository: string;
  branch: string;
  locked: boolean;
  areas: Array<{ path: string; label: string }>;
  revision: string;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [areaPath, setAreaPath] = useState("");
  const selectedArea = areas.some((area) => area.path === areaPath)
    ? areaPath
    : "";
  const key = [
    "auto-improvement-repository-assessment",
    repository,
    branch,
    revision,
    selectedArea,
  ];
  async function request(
    method: "GET" | "POST",
    stop = false,
    area = selectedArea,
  ): Promise<Assessment> {
    const response = await fetch(
      `/api/apps/auto-improvement/repository/assessment${stop ? "/stop" : ""}${method === "GET" ? `?areaPath=${encodeURIComponent(area)}` : ""}`,
      {
        method,
        ...(method === "POST"
          ? {
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(
                area ? { areaPath: area } : {},
              ),
            }
          : {}),
      },
    );
    const data = await response.json();
    if (!response.ok)
      throw new Error(
        data.error || t("testEnvironment.failed", { status: response.status }),
      );
    return data;
  }
  const assessment = useQuery({
    queryKey: key,
    queryFn: () => request("GET"),
    enabled: Boolean(repository),
    refetchInterval: (query) =>
      query.state.data?.state === "assessing" ? 2000 : false,
  });
  const start = useMutation({
    mutationFn: (selection: { key: string[]; area: string }) =>
      request("POST", false, selection.area),
    onSuccess: (data, selection) => qc.setQueryData(selection.key, data),
  });
  const stop = useMutation({
    mutationFn: (selection: { key: string[]; area: string }) =>
      request("POST", true, selection.area),
    onSuccess: (_data, selection) =>
      qc.invalidateQueries({ queryKey: selection.key }),
  });
  const running = start.isPending || assessment.data?.state === "assessing";
  return (
    <section className="space-y-2 text-[12px]">
      <p>{t("repositoryAssessment.agentHint")}</p>
      <SimpleSelect
        options={["", ...areas.map((area) => area.path)]}
        optionLabels={[
          t("repositoryAssessment.all"),
          ...areas.map((area) => area.label),
        ]}
        value={selectedArea}
        onChange={setAreaPath}
        disabled={running}
        aria-label={t("repositoryAssessment.areas")}
      />
      <Btn
        disabled={locked || running || !repository}
        onClick={() => start.mutate({ key, area: selectedArea })}
      >
        {running ? (
          <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
        ) : null}
        {t("repositoryAssessment.assess")}
      </Btn>
      {running ? (
        <Btn
          disabled={stop.isPending || start.isPending}
          onClick={() => stop.mutate({ key, area: selectedArea })}
        >
          {t("repositoryAssessment.stop")}
        </Btn>
      ) : null}
      <p role="status">
        {t(`repositoryAssessment.state_${assessment.data?.state ?? "none"}`)}
      </p>
      {/* No hand-off: unsaved workload and environment settings may be present. */}
      <ErrorNotice
        message={
          assessment.error?.message ||
          start.error?.message ||
          stop.error?.message ||
          assessment.data?.error
        }
      />
      {assessment.data?.result ? (
        <details open>
          <summary className="cursor-pointer">
            {t("repositoryAssessment.findings")}
          </summary>
          <div className="max-h-64 space-y-3 overflow-auto break-words select-text">
            <code>{assessment.data.revision}</code>
            <p>{assessment.data.result.summary}</p>
          {assessment.data.result.partial ? <p>{t("repositoryAssessment.partial")}</p> : null}
            {assessment.data.result.findings.map((finding, index) => (
              <div key={index}>
                <p className="font-medium">{finding.title}</p>
                <p>{finding.detail}</p>
                <code className="block whitespace-pre-wrap">
                  {[
                    ...finding.sourcePaths,
                    ...finding.testPaths,
                    ...finding.evidencePaths,
                  ].join("\n")}
                </code>
                {finding.command ? (
                  <p>
                    <span>{t("repositoryAssessment.unvalidated")}</span>{" "}
                    <code>{finding.command}</code>
                  </p>
                ) : null}
              </div>
            ))}
          </div>
        </details>
      ) : null}
    </section>
  );
}
