"""Inventory retains unmapped areas and explains static evidence without execution."""

from kiro_crew.apps.builtins.auto_improvement.backend.repository_inventory import (
    repository_inventory,
)


def _survey(files):
    entries = {path: ("100644", "unused", len(body)) for path, body in files.items()}
    return repository_inventory(entries, files.__getitem__)


def test_apps_and_unmapped_frontend_remain_visible_with_cross_name_import_mapping():
    result = _survey(
        {
            "apps/billing/apps.py": "raise RuntimeError('never execute')",
            "apps/billing/models.py": "pass",
            "customers/service.py": "pass",
            "spa/src/page.tsx": "export default {}",
            "tests/unit/test_receipts.py": "from apps.billing.models import Receipt",
        }
    )
    areas = {area["path"]: area for area in result["areas"]}
    assert set(areas) == {"apps/billing", "customers", "spa"}
    assert areas["apps/billing"]["kind"] == "django-app"
    assert areas["apps/billing"]["mappingConfidence"] == "imports"
    assert areas["apps/billing"]["testPaths"] == ["tests/unit/test_receipts.py"]
    assert areas["customers"]["mappingConfidence"] == "unmapped"
    assert areas["spa"]["pythonSupported"] is False


def test_directory_evidence_is_distinct_from_imports_and_benchmarks_are_unverified():
    result = _survey(
        {
            "src/orders/service.py": "pass",
            "tests/unit/orders/test_service.py": "pass",
            "benchmarks/orders.py": "raise RuntimeError('never execute')",
        }
    )
    assert result["areas"][0]["mappingConfidence"] == "directory"
    assert result["performanceCandidates"][0]["status"] == "unverified"
    assert "command" not in result["performanceCandidates"][0]


def test_symlinks_never_read_and_oversized_tests_report_incomplete_mapping():
    entries = {
        "orders/model.py": ("100644", "a", 4),
        "tests/test_link.py": ("120000", "b", 12),
        "tests/test_large.py": ("100644", "c", 65537),
    }

    def refuse_read(path):
        raise AssertionError(path)

    result = repository_inventory(entries, refuse_read)
    assert result["areas"][0]["mappingConfidence"] == "unmapped"
    assert result["inventoryLimits"]["truncated"] is True
    assert result["inventoryLimits"]["importFilesSkipped"] == 1


def test_import_scan_cap_is_visible_and_does_not_hide_source_areas():
    files = {f"tests/test_{index:03}.py": "pass" for index in range(257)}
    files["unmapped/code.py"] = "pass"
    result = _survey(files)
    assert result["inventoryLimits"]["importFilesScanned"] == 256
    assert result["inventoryLimits"]["importFilesSkipped"] == 1
    assert result["areas"][0]["path"] == "unmapped"


def test_invalid_test_syntax_does_not_drop_area_and_src_imports_resolve():
    result = _survey(
        {
            "src/orders/service.py": "pass",
            "tests/test_good.py": "import orders.service",
            "tests/test_bad.py": "this is not python!",
        }
    )
    assert result["areas"][0]["mappingConfidence"] == "imports"
    assert result["areas"][0]["testPaths"] == ["tests/test_good.py"]
    assert result["inventoryLimits"]["importFilesSkipped"] == 1


def test_large_test_mapping_reports_truncation_instead_of_unbounded_payload():
    files = {f"tests/test_{index}.py": "import orders.model" for index in range(101)}
    files["orders/model.py"] = "pass"
    result = _survey(files)
    area = result["areas"][0]
    assert len(area["testPaths"]) == 100
    assert area["totalTestPaths"] == 101
    assert area["testPathsTruncated"] is True
    assert result["inventoryLimits"]["truncated"] is True
