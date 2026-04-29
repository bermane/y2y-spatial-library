# Y2Y Spatial Library — Changelog

Append-only audit log. **Never edit past entries. Never regenerate this file.**

Each entry records a single mutation to the library or inventory, in
chronological order. Written by `pipeline.inventory_manager.append_changelog()`.

Format (one block per entry):

```
## YYYY-MM-DDTHH:MM:SSZ — <action> — <dataset_id>
actor:  <command-runner ($USER)>
path:   <library-relative path, or "—" for metadata-only changes>
detail: <one-line human-readable summary>
```

Valid actions: `add`, `update`, `rename`, `remove`, `refresh`, `metadata`, `reconcile-note`.

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

## 2026-04-28T21:30:39Z — add — ds_9a7d4290cb4b
actor:  ethanberman
path:   Species/Grizzly_Bear/gb_habitat_female_fall.tif
detail: Ingested 'GB Habitat Male Summer'; source: GeoTIFF 'GBHabitat_Female_Fall.tif'.

## 2026-04-28T21:30:53Z — add — ds_4d6a5461204f
actor:  ethanberman
path:   Species/Grizzly_Bear/gb_habitat_female_spring.tif
detail: Ingested 'GB Habitat Female Spring'; source: GeoTIFF 'GBHabitat_Female_Spring.tif'.

## 2026-04-28T21:31:08Z — add — ds_796acc649aa8
actor:  ethanberman
path:   Species/Grizzly_Bear/gb_habitat_female_summer.tif
detail: Ingested 'GB Habitat Female Summer'; source: GeoTIFF 'GBHabitat_Female_Summer.tif'.

## 2026-04-28T21:31:22Z — add — ds_d2a0bf9ba94c
actor:  ethanberman
path:   Species/Grizzly_Bear/gb_habitat_male_fall.tif
detail: Ingested 'GB Habitat Male Fall'; source: GeoTIFF 'GBHabitat_Male_Fall.tif'.

## 2026-04-28T21:31:36Z — add — ds_2bf69af60187
actor:  ethanberman
path:   Species/Grizzly_Bear/gb_habitat_male_spring.tif
detail: Ingested 'GB Habitat Male Spring'; source: GeoTIFF 'GBHabitat_Male_Spring.tif'.

## 2026-04-28T21:31:50Z — add — ds_99ae342b49ff
actor:  ethanberman
path:   Species/Grizzly_Bear/gb_habitat_male_summer.tif
detail: Ingested 'GB Habitat Male Summer'; source: GeoTIFF 'GBHabitat_Male_Summer.tif'.

## 2026-04-28T21:31:53Z — add — ds_db8781c83abb
actor:  ethanberman
path:   Water/hydrologic_unit_code_10_watersheds_alberta.gpkg
detail: Ingested 'Hydrologic Unit Code 10 Watersheds of Alberta'; source: Shapefile 'Hydrologic_Unit_Code_10_Watersheds_Alberta.shp'.

## 2026-04-28T21:31:53Z — add — ds_4f4b9081ed0b
actor:  ethanberman
path:   Prot_Areas_Cons_Lands/ross_river_ipca_boundary.gpkg
detail: Ingested 'Ross River IPCA'; source: GeoPackage 'ross_river_ipca_boundary.gpkg'.

## 2026-04-28T21:32:26Z — add — ds_caf4e571ce8e
actor:  ethanberman
path:   Threats_Human_Footprint_Infras/y2y_rec_ecol_linear_features_20230330.gpkg
detail: Ingested 'Y2Y Recreation Ecology Linear Features'; source: GeoPackage 'y2y_rec_ecol_linear_features_20230330.gpkg'.

## 2026-04-28T21:32:26Z — add — ds_1c43c03e8f6b
actor:  ethanberman
path:   Admin_Juris_Boundaries/fortress_mountain_resort_boundary.gpkg
detail: Ingested 'Fortress Mountain Resort Boundary'; source: GeoPackage 'Fortress_Mountain_Resort_Boundary.gpkg'.

## 2026-04-28T21:32:26Z — add — ds_515c22043e40
actor:  ethanberman
path:   Prot_Areas_Cons_Lands/proposed_protected_areas_northern_bc_v2.gpkg
detail: Ingested 'Proposed Protected Areas Northern BC (2025)'; source: GeoPackage 'proposed_pa_v2.gpkg'.

## 2026-04-28T21:42:37Z — add — ds_95a2379e40ad
actor:  ethanberman
path:   Species/Other/dfo_sara_bull_trout.gpkg
detail: Ingested 'Bull Trout Critical Habitat'; source: GeoPackage 'DFO_SARA_BullTrout.gpkg'.

## 2026-04-28T21:42:37Z — add — ds_86c839313033
actor:  ethanberman
path:   Prot_Areas_Cons_Lands/parks_protected_areas_alberta.gpkg
detail: Ingested 'Parks and Protected Areas in Alberta'; source: Shapefile 'parks_protected_areas_alberta.shp'.

## 2026-04-29T17:42:39Z — add — ds_5fec788fce40
actor:  ethanberman
path:   Land_Cover_Use_Disturbance/y2y_land_cover_2020.tif
detail: Ingested 'Y2Y Land Cover (2020)'; source: GeoTIFF 'landcover-2020-classification-y2y.tif'.

