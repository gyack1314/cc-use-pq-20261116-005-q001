from __future__ import annotations

from pathlib import Path
import json
import sqlite3

from climateledger.intake import load_batch, write_manifest
from climateledger.registry import ContractError, FormatSpec
from climateledger.service import LedgerService
from support import IDENTITY_COLUMNS, LedgerCase


V2_COLUMNS = {"indicator": "indicator_id", "period": "month", "value": "reading", "unit": "unit_label"}
V2_UNITS = {"PPB": "ppb"}


class FormatVersionCase(LedgerCase):
    def setUp(self):
        super().setUp()
        self.service.register_format({"source": "lab_a", "version": "v2",
                                      "columns": V2_COLUMNS, "units": V2_UNITS})

    def v2_rows(self, *values, unit="PPB", start=1):
        return [{"indicator_id": "co2_ppm", "month": f"2026-{n:02d}", "reading": str(value),
                 "unit_label": unit} for n, value in enumerate(values, start)]

    def v2_ingest(self, batch_id, rows, format_name="json"):
        return self.ingest(batch_id, rows, format_name=format_name,
                           format_version="v2", columns=V2_COLUMNS)


class FormatContractTests(LedgerCase):
    def test_format_registration_is_idempotent(self):
        spec = {"source": "lab_a", "version": "v9", "columns": IDENTITY_COLUMNS}
        self.service.register_format(spec)
        self.service.register_format(spec)
        self.assertEqual(len(self.service.registry.list_formats("lab_a")), 2)

    def test_format_contract_is_immutable(self):
        self.service.register_format({"source": "lab_a", "version": "v9", "columns": IDENTITY_COLUMNS})
        with self.assertRaisesRegex(ContractError, "format_immutable"):
            self.service.register_format({"source": "lab_a", "version": "v9",
                                          "columns": {**IDENTITY_COLUMNS, "unit": "unit_label"}})
        self.assertEqual(self.service.registry.format_map("lab_a")["v9"].columns, IDENTITY_COLUMNS)

    def test_format_requires_registered_source(self):
        with self.assertRaisesRegex(ContractError, "unknown_source"):
            self.service.register_format({"source": "lab_z", "version": "v1", "columns": IDENTITY_COLUMNS})

    def test_format_columns_must_cover_fields(self):
        broken = {key: key for key in ("indicator", "period", "value")}
        with self.assertRaisesRegex(ContractError, "column_mapping"):
            FormatSpec.from_dict({"source": "lab_a", "version": "v9", "columns": broken})

    def test_format_version_name_validation(self):
        with self.assertRaisesRegex(ContractError, "format_version_name"):
            FormatSpec.from_dict({"source": "lab_a", "version": "bad version!", "columns": IDENTITY_COLUMNS})

    def test_format_units_validation(self):
        with self.assertRaisesRegex(ContractError, "format_units"):
            FormatSpec.from_dict({"source": "lab_a", "version": "v9",
                                  "columns": IDENTITY_COLUMNS, "units": {"": "ppb"}})


class MarkerTests(FormatVersionCase):
    def test_missing_marker_is_not_guessed(self):
        path = self.manifest("nomark", self.rows(428))
        manifest = json.loads(path.read_text(encoding="utf-8"))
        del manifest["format_version"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "format_version_missing"):
            self.service.ingest(path)
        self.assertEqual(self.service.batches(), [])

    def test_unknown_marker_is_not_guessed(self):
        path = self.manifest("badmark", self.rows(428), format_version="v9")
        with self.assertRaisesRegex(ContractError, "format_version_unknown"):
            self.service.ingest(path)
        self.assertEqual(self.service.batches(), [])

    def test_marker_must_be_declared_for_this_source(self):
        path = self.manifest("wrongsrc", self.rows(428), source="lab_b", format_version="v2")
        with self.assertRaisesRegex(ContractError, "format_version_unknown"):
            self.service.ingest(path)

    def test_source_without_declared_formats_cannot_ingest(self):
        self.service.register_source({"key": "lab_c", "title": "Synthetic lab C",
                                      "license": "Local synthetic fixture", "allowed_indicators": ["co2_ppm"]})
        path = self.manifest("noformats", self.rows(428), source="lab_c", format_version="v1")
        with self.assertRaisesRegex(ContractError, "format_version_unknown"):
            self.service.ingest(path)


class TransitionTests(FormatVersionCase):
    def test_dual_format_delivery_yields_same_canonical_values(self):
        first, _ = self.ingest("jan_v1", self.rows(428, 429))
        second, _ = self.v2_ingest("jan_v2", self.v2_rows("428000", "429000"))
        self.assertEqual((first["status"], second["status"]), ("accepted", "accepted"))
        old_rows = self.service.batch(first["id"])["measurements"]
        new_rows = self.service.batch(second["id"])["measurements"]
        self.assertEqual([row["value"] for row in old_rows], [row["value"] for row in new_rows])
        self.assertEqual([row["unit"] for row in new_rows], ["ppm", "ppm"])
        first_release = self.service.publish(first["id"], first["revision"])
        second_release = self.service.publish(second["id"], second["revision"])
        diff = self.service.compare(first_release["version"], second_release["version"])
        self.assertEqual(diff["summary"]["by_kind"]["revised"], 0)
        self.assertEqual(diff["summary"]["by_kind"]["reaffirmed"], 2)

    def test_unit_alias_normalization_records_delivered_label(self):
        batch, _ = self.v2_ingest("alias", self.v2_rows("428000"))
        row = self.service.batch(batch["id"])["measurements"][0]
        self.assertEqual(row["value"], "428.00")
        self.assertEqual(row["raw_unit"], "PPB")
        self.assertEqual(row["raw_value"], "428000")
        self.assertEqual(row["factor"], "0.001")
        self.assertEqual(batch["format_version"], "v2")

    def test_csv_delivery_in_new_format(self):
        path = self.manifest("csv_v2", self.v2_rows("428000", "429000"),
                             format_name="csv", format_version="v2", columns=V2_COLUMNS)
        envelope = load_batch(path, self.formats())
        self.assertEqual(envelope.format_version, "v2")
        self.assertEqual([row["indicator"] for row in envelope.rows], ["co2_ppm", "co2_ppm"])
        batch = self.service.ingest(path)["batch"]
        self.assertEqual(batch["status"], "accepted")
        values = [row["value"] for row in self.service.batch(batch["id"])["measurements"]]
        self.assertEqual(values, ["428.00", "429.00"])

    def test_illegal_rows_in_new_format_reach_quality_gate(self):
        rows = self.v2_rows("428000") + [
            {"indicator_id": "co2_ppm", "month": "2026-02", "reading": "junk", "unit_label": "PPB"},
            {"indicator_id": "co2_ppm", "month": "2026-03", "reading": "429000", "unit_label": "XYZ"}]
        batch, _ = self.v2_ingest("bad_v2", rows)
        self.assertEqual(batch["status"], "rejected")
        codes = {finding["code"] for finding in batch["findings"]}
        self.assertIn("invalid_decimal", codes)
        self.assertIn("unknown_unit", codes)
        with self.assertRaisesRegex(ContractError, "quality_gate"):
            self.service.publish(batch["id"], batch["revision"])

    def test_same_payload_with_different_marker_is_new_revision(self):
        self.service.register_format({"source": "lab_b", "version": "legacy", "columns": IDENTITY_COLUMNS})
        rows = self.rows(428)
        first, _ = self.ingest("same_bytes", rows, source="lab_b", format_version="v1")
        second, second_manifest = self.ingest("same_bytes", rows, source="lab_b", format_version="legacy")
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["format_version"], "legacy")
        self.assertNotEqual(first["id"], second["id"])
        retry = self.service.ingest(second_manifest)
        self.assertTrue(retry["idempotent"])
        self.assertEqual(retry["batch"]["id"], second["id"])


class ProvenanceTests(FormatVersionCase):
    def test_confirmed_version_is_recorded_everywhere(self):
        batch, _ = self.v2_ingest("prov", self.v2_rows("428000"))
        self.assertEqual(batch["format_version"], "v2")
        receipt = self.service.receipts.batch_receipt(batch["id"])
        self.assertEqual(receipt["format_version"], "v2")
        history = self.service.batch_history("lab_a", "prov")
        self.assertEqual(history[0]["format_version"], "v2")
        published = self.service.publish(batch["id"], batch["revision"])
        self.assertEqual(published["manifest"]["batch"]["format_version"], "v2")
        trace = self.service.audit.batch_trace(batch["id"])
        self.assertEqual(trace["batch"]["format_version"], "v2")
        source_receipt = self.service.receipts.source_summary("lab_a")
        self.assertEqual({spec["version"] for spec in source_receipt["formats"]}, {"v1", "v2"})

    def test_historical_release_export_is_stable_across_migration(self):
        _, release, _ = self.release()
        before = self.service.export(release["version"])
        second, _ = self.v2_ingest("post_switch", self.v2_rows("430000", start=3))
        self.service.publish(second["id"], second["revision"])
        after = self.service.export(release["version"])
        self.assertEqual(before, after)
        self.assertTrue(self.service.verify(after))


class ReplayTests(FormatVersionCase):
    def test_replay_confirms_recorded_version(self):
        batch, manifest = self.v2_ingest("replay_v2", self.v2_rows("428000", "429000"))
        result = self.service.replay(manifest, batch["id"])
        self.assertTrue(result["reproducible"])
        self.assertTrue(result["format_version_match"])
        self.assertEqual(result["format_version"], "v2")
        self.assertEqual(result["replayed_format_version"], "v2")

    def test_replay_with_swapped_marker_cannot_reinterpret(self):
        batch, manifest = self.v2_ingest("swap", self.v2_rows("428000"))
        tampered = json.loads(manifest.read_text(encoding="utf-8"))
        tampered["format_version"] = "v1"
        del tampered["columns"]
        manifest.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "missing_column"):
            self.service.replay(manifest, batch["id"])

    def test_manifest_receipt_carries_format_version(self):
        _, manifest = self.v2_ingest("receipt_v2", self.v2_rows("428000"))
        receipt = self.service.replay_inspector.manifest_receipt(manifest)
        self.assertEqual(receipt["format_version"], "v2")
        self.assertTrue(receipt["same_payload"])


class LegacyMigrationTests(LedgerCase):
    def test_database_without_format_version_migrates(self):
        legacy = self.root / "legacy.sqlite3"
        connection = sqlite3.connect(legacy)
        connection.execute(
            "CREATE TABLE batches (id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,"
            " revision INTEGER NOT NULL, payload_hash TEXT NOT NULL, status TEXT NOT NULL, row_count INTEGER NOT NULL,"
            " metadata TEXT NOT NULL, findings TEXT NOT NULL, received_at TEXT NOT NULL,"
            " approved_at TEXT, published_version INTEGER, UNIQUE(source, external_id, revision))")
        connection.execute(
            "INSERT INTO batches(source,external_id,revision,payload_hash,status,row_count,metadata,findings,received_at)"
            " VALUES('lab_a','old_batch',1,'abc123','published',1,'{}','[]','2026-01-01T00:00:00+00:00')")
        connection.commit()
        connection.close()
        with LedgerService(legacy) as service:
            old = service.store.batch(1)
            self.assertIsNone(old["format_version"])
            service.register_indicator({"key": "co2_ppm", "title": "Synthetic carbon dioxide",
                                        "canonical_unit": "ppm", "cadence": "month", "precision": 2})
            service.register_source({"key": "lab_a", "title": "Synthetic lab A",
                                     "license": "Local synthetic fixture", "allowed_indicators": ["co2_ppm"]})
            service.register_format({"source": "lab_a", "version": "v1", "columns": IDENTITY_COLUMNS})
            manifest = write_manifest(self.root / "legacy_in", source="lab_a", batch_id="new_batch",
                                      rows=[{"indicator": "co2_ppm", "period": "2026-01",
                                             "value": "428", "unit": "ppm"}], format_version="v1")
            batch = service.ingest(manifest)["batch"]
            self.assertEqual(batch["format_version"], "v1")
            report = service.audit_all()
            self.assertEqual(report["batch_count"], 2)
            self.assertEqual(report["violations"], [])
