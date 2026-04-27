# Y2Y Spatial Library — Changelog

Append-only audit log. **Never edit past entries. Never regenerate this file.**

Each entry records a single mutation to the library or inventory, in
chronological order. Written by `pipeline.inventory_manager.append_changelog()`.

Format (one block per entry):

```
## YYYY-MM-DDTHH:MM:SSZ — <action> — <dataset_id>
actor:  <data_steward>
path:   <library-relative path, or "—" for metadata-only changes>
detail: <one-line human-readable summary>
```

Valid actions: `add`, `update`, `rename`, `remove`, `metadata`, `reconcile-note`.

---

<!-- entries below this line, most recent last -->
## 2026-04-27T21:32:00Z — add — ds_f874441ec27d
actor:  Ethan
path:   Admin_Juris_Boundaries/y2y_region_boundary_2013.gpkg
detail: Ingested 'Y2Y Region Boundary (2013)'; source: Shapefile 'Y2Y_RegionBoundary.shp'.

## 2026-04-27T21:35:05Z — update — ds_f874441ec27d
actor:  ethanberman
path:   Admin_Juris_Boundaries/y2y_region_boundary_2013.gpkg
detail: terms_of_use: 'none' → 'Public domain — no restrictions on use.'

## 2026-04-27T22:02:03Z — add — ds_38c71dd02669
actor:  ethanberman
path:   Climate_Resilience/total_biomass_2022_t_ha.tif
detail: Ingested 'Biomass Carbon Density (t/ha) – 2022'; source: GeoTIFF 'total_biomass_2022_t_ha.tif'.

## 2026-04-27T22:08:39Z — add — ds_3ea606fb1fae
actor:  ethanberman
path:   Prot_Areas_Cons_Lands/y2y_protected_areas_2025.gpkg
detail: Ingested 'Y2Y Protected Areas (2025)'; source: GeoPackage 'y2y_protected_areas.gpkg'.

