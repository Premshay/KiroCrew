import { useTranslation } from "react-i18next";
import { useState } from "react";
import { Input } from "../../components/ui";
import { fmtNumber } from "../../i18n/format";

export interface RepositoryInventoryData {
  areas?: Array<{
    id: string;
    path: string;
    label: string;
    kind: string;
    sourceFiles: number;
    testPaths: string[];
    mappingConfidence: string;
    evidence: string[];
    pythonSupported?: boolean;
    totalTestPaths?: number;
    testPathsTruncated?: boolean;
  }>;
  performanceCandidates?: Array<{
    path: string;
    evidence: string[];
    status: string;
  }>;
  inventoryLimits?: {
    truncated: boolean;
    limitations: string[];
    totalAreas: number;
    importFilesScanned: number;
    importFilesSkipped: number;
    totalPerformanceCandidates: number;
  };
}

/** Source and test associations are neither coverage nor improvement rankings. */
export default function RepositoryInventory({
  data,
}: {
  data: RepositoryInventoryData;
}) {
  const { t } = useTranslation();
  const [search, setSearch] = useState("");
  const areas = data.areas ?? [];
  const visible = areas.filter((area) =>
    `${area.label} ${area.path}`.toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <div className="space-y-2 text-[12px]">
      <details open>
        <summary className="cursor-pointer">
          {t("repositoryAssessment.areas")} ({fmtNumber(areas.length)})
        </summary>
        <Input
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder={t("repositoryAssessment.search")}
          aria-label={t("repositoryAssessment.search")}
        />
        <div className="max-h-64 space-y-3 overflow-auto break-words select-text">
          {visible.map((area) => (
            <details key={area.id}>
              <summary className="cursor-pointer font-medium">
                {area.label} <code>{area.path}</code>
              </summary>
              <p>
                {t("repositoryAssessment.mapping", {
                  confidence: t(
                    `repositoryAssessment.mapping_${area.mappingConfidence}`,
                  ),
                })}
              </p>
              <p>
                {t("repositoryAssessment.sourceFiles", {
                  total: fmtNumber(area.sourceFiles),
                })}
              </p>
              {area.pythonSupported === false ? (
                <p>{t("repositoryAssessment.unsupported")}</p>
              ) : null}
              {area.testPathsTruncated ? (
                <p>{t("repositoryAssessment.partial")}</p>
              ) : null}
              {area.testPaths.length ? (
                <code className="whitespace-pre-wrap">
                  {area.testPaths.join("\n")}
                </code>
              ) : (
                <p>{t("repositoryAssessment.unmapped")}</p>
              )}
              {area.evidence.map((evidence, index) => (
                <p key={index} className="text-muted">
                  {evidence}
                </p>
              ))}
            </details>
          ))}
        </div>
      </details>
      <details>
        <summary className="cursor-pointer">
          {t("repositoryAssessment.performance")}
        </summary>
        <p>{t("repositoryAssessment.unverified")}</p>
        <div className="max-h-48 space-y-2 overflow-auto break-words select-text">
          {(data.performanceCandidates ?? []).map((candidate) => (
            <div key={candidate.path}>
              <code>{candidate.path}</code>
              {candidate.evidence.map((evidence, index) => (
                <p key={index}>{evidence}</p>
              ))}
            </div>
          ))}
        </div>
      </details>
      {data.inventoryLimits ? (
        <details>
          <summary className="cursor-pointer">
            {t("repositoryAssessment.limits")}
          </summary>
          {data.inventoryLimits.truncated ? (
            <p>{t("repositoryAssessment.partial")}</p>
          ) : null}
          <p>{t("repositoryAssessment.scanCounts", {
            shown: fmtNumber(areas.length), total: fmtNumber(data.inventoryLimits.totalAreas),
            read: fmtNumber(data.inventoryLimits.importFilesScanned), skipped: fmtNumber(data.inventoryLimits.importFilesSkipped),
            candidates: fmtNumber(data.inventoryLimits.totalPerformanceCandidates),
          })}</p>
          {data.inventoryLimits.limitations.map((limit, index) => (
            <p key={index} className="text-muted">
              {limit}
            </p>
          ))}
        </details>
      ) : null}
    </div>
  );
}
