"""Default symbology on first publish, and ``agol-sync restyle``.

No test here contacts ArcGIS Online: AGOL items are small fakes that
record what was written to them.
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest
import rasterio
from click.testing import CliRunner

from pipeline import agol_config, agol_sync, inventory_manager, taxonomy
from pipeline import agol_symbology as sym

from .test_agol_push import _full_row, _make_gis

BLUE = (0, 114, 178)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

@pytest.fixture
def _config_no_cache() -> agol_config.AgolConfig:
    """A config with the group id already known, so no group search runs."""
    return agol_config.AgolConfig(conservation_atlas_group_id="cached_group_xyz")


class _Resources:
    def __init__(self, saved: dict[str, Any] | None = None) -> None:
        self.saved = saved
        self.calls: list[str] = []

    def get(self, file: str, try_json: bool = True):
        if self.saved is None:
            raise RuntimeError("resource does not exist")
        return self.saved

    def add(self, *, folder_name: str, file_name: str, text: str):
        if self.saved is not None:
            raise RuntimeError("resource already exists")
        self.calls.append("add")
        assert (folder_name, file_name) == ("styles", "root.json")
        self.saved = json.loads(text)

    def update(self, *, folder_name: str, file_name: str, text: str):
        self.calls.append("update")
        self.saved = json.loads(text)


class _Item:
    """Stands in for an arcgis ``Item``."""

    def __init__(
        self, item_type: str, *, data: Any = None,
        saved_style: dict[str, Any] | None = None, accepts: bool = True,
    ) -> None:
        self.id = "item_abc"
        self.type = item_type
        self.url = "https://tiles.example.com/arcgis/rest/services/x/VectorTileServer"
        self._data = data
        self._accepts = accepts
        self.resources = _Resources(saved_style)
        self.updates: list[dict[str, Any]] = []

    def get_data(self):
        return self._data

    def update(self, item_properties=None, **kwargs):
        if not self._accepts:
            raise RuntimeError("AGOL said no")
        self.updates.append(item_properties)
        if item_properties and "text" in item_properties:
            self._data = json.loads(item_properties["text"])
        return True


def _patch_feature_layers(monkeypatch, layers: list[tuple[int, str | None]]):
    fake_layers = []
    for layer_id, geometry in layers:
        layer = MagicMock()
        layer.properties = {"id": layer_id, "geometryType": geometry}
        fake_layers.append(layer)
    collection = MagicMock()
    collection.layers = fake_layers
    flc_class = MagicMock()
    flc_class.fromitem.return_value = collection
    fake_features = MagicMock()
    fake_features.FeatureLayerCollection = flc_class
    monkeypatch.setitem(sys.modules, "arcgis.features", fake_features)


def _write_vtpk(path: Path, layers: list[dict[str, Any]]) -> Path:
    style = {
        "version": 8,
        "sprite": "../sprites/sprite",
        "glyphs": "../fonts/{fontstack}/{range}.pbf",
        "sources": {"esri": {"type": "vector", "url": "../../"}},
        "layers": layers,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("p12/resources/styles/root.json", json.dumps(style))
        package.writestr("p12/tile/L00/R0000C0000.bundle", b"")
    return path


def _pro_fill(name: str = "main.parks") -> dict[str, Any]:
    """The single rule ArcGIS Pro writes for a single-symbol polygon layer."""
    return {
        "id": name, "type": "fill", "source": "esri", "source-layer": name,
        "layout": {},
        "paint": {"fill-color": "#C5EBFC", "fill-outline-color": "#6E6E6E"},
    }


def _write_raster(
    path: Path, data: np.ndarray, *, nodata, colormap=None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff", "dtype": str(data.dtype), "count": 1,
        "width": data.shape[1], "height": data.shape[0],
        "crs": "ESRI:102008",
        "transform": rasterio.transform.from_origin(0, 0, 30, 30),
        "nodata": nodata, "tiled": True, "blockxsize": 256, "blockysize": 256,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
        if colormap:
            dst.write_colormap(1, colormap)
    return path


# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------

def test_every_category_has_a_colour() -> None:
    assert set(sym.CATEGORY_COLOURS) == set(taxonomy.CATEGORIES)
    for value in sym.CATEGORY_COLOURS.values():
        assert len(sym.hex_to_rgb(value)) == 3


def test_category_colours_are_all_different() -> None:
    assert len(set(sym.CATEGORY_COLOURS.values())) == len(sym.CATEGORY_COLOURS)


def test_unknown_category_falls_back() -> None:
    assert sym.category_colour("Not a category") == sym.hex_to_rgb(sym.FALLBACK_COLOUR)
    assert sym.category_colour(None) == sym.hex_to_rgb(sym.FALLBACK_COLOUR)


def test_discrete_palette_has_no_repeats() -> None:
    colours = sym.palette_for(sym.MAX_PALETTE_CLASSES)
    assert len(set(colours)) == sym.MAX_PALETTE_CLASSES
    assert colours[:8] == [sym.hex_to_rgb(c) for c in sym.OKABE_ITO]


# --------------------------------------------------------------------------
# Feature layers
# --------------------------------------------------------------------------

def test_polygon_renderer_is_outline_only() -> None:
    renderer = sym.feature_renderer("esriGeometryPolygon", BLUE)
    symbol = renderer["symbol"]
    assert renderer["type"] == "simple"
    assert symbol["type"] == "esriSFS"
    assert symbol["color"][3] == 0, "polygon fill must be fully transparent"
    assert symbol["outline"]["color"] == [0, 114, 178, 255]
    assert symbol["outline"]["width"] == sym.LINE_WIDTH_PT


def test_line_and_point_renderers_use_the_category_colour() -> None:
    line = sym.feature_renderer("esriGeometryPolyline", BLUE)["symbol"]
    point = sym.feature_renderer("esriGeometryPoint", BLUE)["symbol"]
    assert line["type"] == "esriSLS" and line["color"] == [0, 114, 178, 255]
    assert point["type"] == "esriSMS" and point["color"] == [0, 114, 178, 255]
    assert sym.feature_renderer("esriGeometryMultipoint", BLUE)["symbol"]["type"] == "esriSMS"


def test_tables_get_no_renderer() -> None:
    assert sym.feature_renderer(None, BLUE) is None


def test_feature_item_data_keeps_what_is_already_saved() -> None:
    existing = {
        "layers": [{
            "id": 0,
            "popupInfo": {"title": "keep me"},
            "layerDefinition": {
                "minScale": 5000,
                "drawingInfo": {"labelingInfo": ["keep"], "renderer": {"old": 1}},
            },
        }],
        "tables": ["keep"],
    }
    data = sym.build_feature_item_data(
        existing, [(0, "esriGeometryPolygon"), (1, "esriGeometryPoint")], BLUE,
    )
    first = data["layers"][0]
    assert first["popupInfo"] == {"title": "keep me"}
    # A saved scale range is replaced by "all zoom levels".
    assert first["layerDefinition"]["minScale"] == 0
    assert first["layerDefinition"]["maxScale"] == 0
    assert data["layers"][1]["layerDefinition"]["minScale"] == 0
    assert first["layerDefinition"]["drawingInfo"]["labelingInfo"] == ["keep"]
    assert first["layerDefinition"]["drawingInfo"]["renderer"]["type"] == "simple"
    assert data["tables"] == ["keep"]
    assert data["layers"][1]["id"] == 1
    # The caller's object is not modified.
    assert existing["layers"][0]["layerDefinition"]["drawingInfo"]["renderer"] == {"old": 1}


def test_feature_item_data_needs_a_drawable_layer() -> None:
    with pytest.raises(sym.SymbologyError):
        sym.build_feature_item_data({}, [(0, None)], BLUE)


# --------------------------------------------------------------------------
# Rasters
# --------------------------------------------------------------------------

def test_continuous_raster_gets_a_fixed_viridis_stretch(tmp_path: Path) -> None:
    data = np.linspace(0, 100, 200 * 200, dtype="float32").reshape(200, 200)
    data[0, :] = -9999
    path = _write_raster(tmp_path / "c.tif", data, nodata=-9999)

    renderer, notes = sym.raster_renderer(path, "continuous")

    assert notes == []
    assert renderer["type"] == "rasterStretch"
    assert renderer["stretchType"] == "minMax"
    assert renderer["dra"] is False
    low, high, mean, _std = renderer["statistics"][0]
    assert 0 < low < 10, "NoData must not drag the low end down"
    assert 90 < high < 100
    assert low < mean < high
    ramp = renderer["colorRamp"]
    assert ramp["type"] == "multipart"
    assert ramp["colorRamps"][0]["fromColor"] == [68, 1, 84, 255]
    assert ramp["colorRamps"][-1]["toColor"] == [253, 231, 37, 255]


def test_constant_raster_still_gets_a_valid_stretch(tmp_path: Path) -> None:
    data = np.full((64, 64), 5, dtype="float32")
    path = _write_raster(tmp_path / "flat.tif", data, nodata=-9999)
    low, high, *_ = sym.raster_renderer(path, "continuous")[0]["statistics"][0]
    assert high > low


def test_categorical_raster_without_colour_table_uses_okabe_ito(tmp_path: Path) -> None:
    data = np.full((64, 64), 255, dtype="uint8")
    data[:10] = 3
    data[10:20] = 1
    data[20:30] = 7
    path = _write_raster(tmp_path / "k.tif", data, nodata=255)

    renderer, notes = sym.raster_renderer(path, "categorical")

    assert notes == []
    assert renderer["type"] == "rasterColormap"
    infos = renderer["colormapInfos"]
    assert [i["value"] for i in infos] == [1, 3, 7], "NoData is not a class"
    assert [i["color"][:3] for i in infos] == [
        list(sym.hex_to_rgb(c)) for c in sym.OKABE_ITO[:3]
    ]


def test_categorical_raster_keeps_its_own_colour_table(tmp_path: Path) -> None:
    data = np.full((64, 64), 255, dtype="uint8")
    data[:30] = 1
    data[30:] = 2
    own = {1: (10, 20, 30, 255), 2: (200, 100, 50, 255)}
    path = _write_raster(tmp_path / "lc.tif", data, nodata=255, colormap=own)

    renderer, _ = sym.raster_renderer(path, "categorical")

    assert [i["color"] for i in renderer["colormapInfos"]] == [
        [10, 20, 30, 255], [200, 100, 50, 255],
    ]


def test_many_classes_warn_then_give_up(tmp_path: Path) -> None:
    def raster_with(count: int) -> Path:
        data = (np.arange(64 * 64) % count).astype("uint8").reshape(64, 64)
        return _write_raster(tmp_path / f"n{count}.tif", data, nodata=255)

    _, notes = sym.raster_renderer(raster_with(12), "categorical")
    assert notes and "colour-blind" in notes[0]

    with pytest.raises(sym.NoStandardStyle):
        sym.raster_renderer(raster_with(sym.MAX_PALETTE_CLASSES + 1), "categorical")


def test_imagery_item_data_sets_renderer_and_transparency() -> None:
    data = sym.build_imagery_item_data(
        {"popupInfo": {"keep": 1}}, {"type": "rasterStretch"},
    )
    assert data["popupInfo"] == {"keep": 1}
    assert data["layerDefinition"]["drawingInfo"]["renderer"] == {"type": "rasterStretch"}
    assert data["opacity"] == 0.7
    assert data["minScale"] == 0 and data["maxScale"] == 0


# --------------------------------------------------------------------------
# Vector tile styles
# --------------------------------------------------------------------------

def test_single_fill_rule_counts_as_single_symbol() -> None:
    assert sym.is_single_symbol_style({"layers": [_pro_fill()]})
    # Pro writes a second, line rule when the polygon outline is wide.
    outline = {
        "id": "main.parks/1", "type": "line", "source": "esri",
        "source-layer": "main.parks", "paint": {"line-color": "#000"},
    }
    assert sym.is_single_symbol_style({"layers": [_pro_fill(), outline]})


@pytest.mark.parametrize("change", [
    {"filter": ["==", "TYPE", "Park"]},
    {"paint": {"fill-color": {"property": "TYPE", "stops": [[1, "#fff"]]}}},
    {"type": "symbol", "layout": {"text-field": "{NAME}"}},
])
def test_attribute_styling_is_not_single_symbol(change: dict[str, Any]) -> None:
    rule = {**_pro_fill(), **change}
    assert not sym.is_single_symbol_style({"layers": [rule]})


def test_several_class_rules_are_not_single_symbol() -> None:
    rules = [
        {**_pro_fill(), "id": f"main.parks/{n}"} for n in ("a", "b", "c")
    ]
    assert not sym.is_single_symbol_style({"layers": rules})
    assert not sym.is_single_symbol_style({"layers": []})


def test_standard_vector_tile_style_outlines_polygons(tmp_path: Path) -> None:
    rule = {**_pro_fill(), "minzoom": 4}
    vtpk = _write_vtpk(tmp_path / "a.vtpk", [rule])
    url = "https://tiles.example.com/arcgis/rest/services/x/VectorTileServer"

    style = sym.standard_vector_tile_style(sym.read_vtpk_style(vtpk), BLUE, url)

    fill, outline = style["layers"]
    assert fill["type"] == "fill"
    assert fill["paint"] == {"fill-color": "rgba(0,0,0,0)"}
    assert outline["type"] == "line"
    assert outline["source-layer"] == "main.parks"
    assert outline["paint"]["line-color"] == "#0072B2"
    # Zoom limits from Pro are dropped: visible at all zoom levels.
    assert "minzoom" not in fill and "minzoom" not in outline
    assert "maxzoom" not in fill and "maxzoom" not in outline
    # The copy lives on the item, so its links must be absolute.
    assert style["sources"]["esri"]["url"] == url
    assert style["sprite"] == f"{url}/resources/sprites/sprite"
    assert style["glyphs"] == f"{url}/resources/fonts/{{fontstack}}/{{range}}.pbf"


def test_standard_vector_tile_style_for_lines_and_points() -> None:
    style = {
        "sources": {"esri": {"url": "../../"}},
        "layers": [
            {"id": "roads", "type": "line", "source": "esri",
             "source-layer": "roads", "paint": {"line-color": "#123456"}},
            {"id": "sites", "type": "symbol", "source": "esri",
             "source-layer": "sites", "layout": {"icon-image": "sites"}},
        ],
    }
    out = sym.standard_vector_tile_style(style, BLUE, None)
    assert [r["type"] for r in out["layers"]] == ["line", "circle"]
    assert out["layers"][1]["paint"]["circle-color"] == "#0072B2"
    # No service URL known: relative links are left as they were.
    assert out["sources"]["esri"]["url"] == "../../"


def test_attribute_styled_package_is_refused() -> None:
    style = {"layers": [{**_pro_fill(), "filter": ["==", "TYPE", "Park"]}]}
    with pytest.raises(sym.NoStandardStyle):
        sym.standard_vector_tile_style(style, BLUE, None)


def test_read_vtpk_style_reports_a_package_without_a_style(tmp_path: Path) -> None:
    path = tmp_path / "empty.vtpk"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("p12/tile/x", b"")
    with pytest.raises(sym.SymbologyError):
        sym.read_vtpk_style(path)
    (tmp_path / "junk.vtpk").write_bytes(b"not a zip")
    with pytest.raises(sym.SymbologyError):
        sym.read_vtpk_style(tmp_path / "junk.vtpk")


# --------------------------------------------------------------------------
# apply_default_style
# --------------------------------------------------------------------------

def _apply(item, row, target, *, source_path=Path("x"), vtpk_path=None):
    warnings: list[str] = []
    wrote = sym.apply_default_style(
        item=item, row=row, target=target, source_path=source_path,
        vtpk_path=vtpk_path, warn=warnings.append,
    )
    return wrote, warnings


def test_apply_writes_feature_layer_style(monkeypatch) -> None:
    _patch_feature_layers(monkeypatch, [(0, "esriGeometryPolygon")])
    item = _Item("Feature Service", data=None)

    wrote, warnings = _apply(
        item, {"dataset_id": "ds_1", "category": "Water"}, "feature-layer",
    )

    assert wrote and warnings == []
    renderer = item.get_data()["layers"][0]["layerDefinition"]["drawingInfo"]["renderer"]
    assert renderer["symbol"]["outline"]["color"] == [0, 114, 178, 255]


def test_apply_skips_an_item_that_is_not_the_expected_layer(monkeypatch) -> None:
    """The feature-layer fallback leaves a GeoPackage item: nothing to style."""
    item = _Item("GeoPackage")
    wrote, warnings = _apply(item, {"category": "Water"}, "feature-layer")
    assert not wrote and warnings == [] and item.updates == []


def test_apply_turns_a_failure_into_a_warning(monkeypatch) -> None:
    _patch_feature_layers(monkeypatch, [(0, "esriGeometryPolygon")])
    item = _Item("Feature Service", accepts=False)

    wrote, warnings = _apply(
        item, {"dataset_id": "ds_1", "category": "Water"}, "feature-layer",
    )

    assert not wrote
    assert len(warnings) == 1
    assert "agol-sync restyle ds_1" in warnings[0]


def test_apply_writes_imagery_style(tmp_path: Path) -> None:
    data = np.linspace(0, 1, 64 * 64, dtype="float32").reshape(64, 64)
    path = _write_raster(tmp_path / "c.tif", data, nodata=-9999)
    item = _Item("Image Service", data={})

    wrote, warnings = _apply(
        item, {"category": "Species", "classification": "continuous"},
        "imagery-layer", source_path=path,
    )

    assert wrote and warnings == []
    saved = item.get_data()
    assert saved["opacity"] == 0.7
    assert saved["layerDefinition"]["drawingInfo"]["renderer"]["type"] == "rasterStretch"


def test_apply_writes_vector_tile_style_as_an_item_resource(tmp_path: Path) -> None:
    vtpk = _write_vtpk(tmp_path / "a.vtpk", [_pro_fill()])
    before = vtpk.read_bytes()
    item = _Item("Vector Tile Service")

    wrote, warnings = _apply(
        item, {"category": "Water"}, "vector-tile-layer", vtpk_path=vtpk,
    )

    assert wrote and warnings == []
    assert item.resources.calls == ["add"]
    assert [r["type"] for r in item.resources.saved["layers"]] == ["fill", "line"]
    assert vtpk.read_bytes() == before, "the library package is never modified"


def test_apply_leaves_an_attribute_styled_package_alone(tmp_path: Path) -> None:
    vtpk = _write_vtpk(
        tmp_path / "a.vtpk", [{**_pro_fill(), "filter": ["==", "T", "x"]}],
    )
    item = _Item("Vector Tile Service")

    wrote, warnings = _apply(
        item, {"category": "Water"}, "vector-tile-layer", vtpk_path=vtpk,
    )

    assert not wrote
    assert item.resources.saved is None
    assert len(warnings) == 1 and "kept as built in ArcGIS Pro" in warnings[0]


# --------------------------------------------------------------------------
# push: first publish only
# --------------------------------------------------------------------------

def test_push_styles_a_new_layer_and_never_an_existing_one(
    project_tree, valid_gpkg_factory, _config_no_cache, monkeypatch,
) -> None:
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    inventory_manager.insert_dataset(
        db, _full_row(dataset_id="ds_test", file_path="Water/v.gpkg"),
    )
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sym, "apply_default_style", lambda **kw: calls.append(kw) or True,
    )

    gis = _make_gis(new_item_id="new_fl_id")
    agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )
    assert len(calls) == 1
    assert calls[0]["target"] == "feature-layer"
    assert calls[0]["row"]["dataset_id"] == "ds_test"

    # Second push of the same, now published, row: the style is left alone.
    existing = MagicMock()
    existing.id = "new_fl_id"
    gis2 = _make_gis(existing_item=existing)
    agol_sync.push(
        db, "ds_test", gis2, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )
    assert len(calls) == 1


def test_push_records_a_style_warning_without_failing(
    project_tree, valid_gpkg_factory, _config_no_cache, monkeypatch,
) -> None:
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    inventory_manager.insert_dataset(
        db, _full_row(dataset_id="ds_test", file_path="Water/v.gpkg"),
    )

    def _warns(**kwargs):
        kwargs["warn"]("default style could not be set")
        return False

    monkeypatch.setattr(sym, "apply_default_style", _warns)
    result = agol_sync.push(
        db, "ds_test", _make_gis(), _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )
    assert result.sync_status_after == "clean"
    notes = inventory_manager.get_dataset(db, "ds_test")["internal_notes"]
    assert "[agol] default style could not be set" in notes


# --------------------------------------------------------------------------
# restyle
# --------------------------------------------------------------------------

def _published_row(project_tree, valid_gpkg_factory, **overrides) -> Path:
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    row = _full_row(dataset_id="ds_test", file_path="Water/v.gpkg")
    row.update({"agol_item_id": "item_abc", "sync_status": "clean"})
    row.update(overrides)
    inventory_manager.insert_dataset(db, row)
    return db


def _gis_with(item) -> MagicMock:
    gis = MagicMock()
    gis.content.get.return_value = item
    return gis


def test_restyle_backs_up_then_resets_the_style(
    project_tree, valid_gpkg_factory, monkeypatch,
) -> None:
    db = _published_row(project_tree, valid_gpkg_factory)
    _patch_feature_layers(monkeypatch, [(0, "esriGeometryPolygon")])
    manual = {"layers": [{
        "id": 0, "popupInfo": {"title": "mine"},
        "layerDefinition": {"drawingInfo": {"renderer": {"type": "custom"}}},
    }]}
    item = _Item("Feature Service", data=manual)
    reports = project_tree["root"] / "reports"
    before = dict(inventory_manager.get_dataset(db, "ds_test"))

    backup, note = sym.restyle(
        db, "ds_test", _gis_with(item),
        library_root=project_tree["library"], reports_dir=reports,
        actor="tester",
    )

    saved = json.loads(backup.read_text())
    assert backup.parent == reports
    assert backup.name.startswith("restyle_ds_test_") and backup.suffix == ".json"
    assert saved["style"] == manual
    layer = item.get_data()["layers"][0]
    assert layer["layerDefinition"]["drawingInfo"]["renderer"]["type"] == "simple"
    assert layer["popupInfo"] == {"title": "mine"}, "only the drawing style changes"
    assert backup.name in note
    assert dict(inventory_manager.get_dataset(db, "ds_test")) == before


def test_restyle_leaves_the_layer_alone_when_the_backup_fails(
    project_tree, valid_gpkg_factory, monkeypatch,
) -> None:
    db = _published_row(project_tree, valid_gpkg_factory)
    _patch_feature_layers(monkeypatch, [(0, "esriGeometryPolygon")])
    item = _Item("Feature Service", data={"layers": []})
    blocker = project_tree["root"] / "reports"
    blocker.write_text("a file where the folder should be")

    with pytest.raises(agol_sync.AgolError, match="left unchanged"):
        sym.restyle(
            db, "ds_test", _gis_with(item),
            library_root=project_tree["library"], reports_dir=blocker,
            actor="tester",
        )
    assert item.updates == []


def test_restyle_refuses_an_unpublished_row(project_tree, valid_gpkg_factory) -> None:
    db = _published_row(
        project_tree, valid_gpkg_factory,
        agol_item_id=None, sync_status="unpublished",
    )
    with pytest.raises(agol_sync.AgolError, match="not published"):
        sym.restyle(
            db, "ds_test", MagicMock(),
            library_root=project_tree["library"],
            reports_dir=project_tree["root"] / "reports", actor="tester",
        )


def test_restyle_refuses_an_attribute_styled_vector_tile_layer(
    project_tree, valid_gpkg_factory,
) -> None:
    db = _published_row(
        project_tree, valid_gpkg_factory, agol_format="vector-tile-layer",
    )
    from pipeline import agol_vtpk
    row = inventory_manager.get_dataset(db, "ds_test")
    vtpk = agol_vtpk.resolve_vtpk_path(row, project_tree["library"])
    _write_vtpk(vtpk, [{**_pro_fill(), "filter": ["==", "T", "x"]}])
    item = _Item("Vector Tile Service", saved_style={"layers": [{"id": "mine"}]})
    reports = project_tree["root"] / "reports"

    with pytest.raises(agol_sync.AgolError, match="not restyled"):
        sym.restyle(
            db, "ds_test", _gis_with(item),
            library_root=project_tree["library"], reports_dir=reports,
            actor="tester",
        )
    assert item.resources.calls == []
    assert not reports.exists() or not list(reports.iterdir())


def test_restyle_replaces_a_saved_vector_tile_style(
    project_tree, valid_gpkg_factory,
) -> None:
    db = _published_row(
        project_tree, valid_gpkg_factory, agol_format="vector-tile-layer",
    )
    from pipeline import agol_vtpk
    row = inventory_manager.get_dataset(db, "ds_test")
    _write_vtpk(
        agol_vtpk.resolve_vtpk_path(row, project_tree["library"]), [_pro_fill()],
    )
    manual = {"layers": [{"id": "mine", "type": "fill", "source-layer": "main.parks"}]}
    item = _Item("Vector Tile Service", saved_style=manual)

    backup, _ = sym.restyle(
        db, "ds_test", _gis_with(item),
        library_root=project_tree["library"],
        reports_dir=project_tree["root"] / "reports", actor="tester",
    )

    assert json.loads(backup.read_text())["style"] == manual
    assert item.resources.calls == ["update"]
    assert [r["type"] for r in item.resources.saved["layers"]] == ["fill", "line"]


def test_restyle_cli_asks_first_and_can_be_cancelled(
    project_tree, valid_gpkg_factory, monkeypatch,
) -> None:
    from pipeline.__main__ import cli

    _published_row(project_tree, valid_gpkg_factory)
    called: list[str] = []
    monkeypatch.setattr(sym, "restyle", lambda *a, **k: called.append("x"))

    result = CliRunner().invoke(
        cli,
        ["--root", str(project_tree["root"]), "agol-sync", "restyle", "ds_test"],
        input="n\n",
    )

    assert result.exit_code == 0, result.output
    assert "cancelled" in result.output
    assert called == []


def test_restyle_cli_yes_runs_without_asking(
    project_tree, valid_gpkg_factory, monkeypatch,
) -> None:
    from pipeline import agol_config
    from pipeline.__main__ import cli

    _published_row(project_tree, valid_gpkg_factory)
    monkeypatch.setattr(agol_config, "load_config", lambda: MagicMock())
    monkeypatch.setattr(agol_sync, "get_gis", lambda cfg: MagicMock())
    backup = project_tree["root"] / "reports" / "restyle_ds_test_x.json"
    monkeypatch.setattr(sym, "restyle", lambda *a, **k: (backup, "reset"))

    result = CliRunner().invoke(
        cli,
        ["--root", str(project_tree["root"]), "agol-sync", "restyle",
         "ds_test", "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert "restyle complete" in result.output


# --------------------------------------------------------------------------
# Rebuilt vector tile package
# --------------------------------------------------------------------------

def test_rebuilt_package_with_renamed_layers_warns(tmp_path: Path) -> None:
    vtpk = _write_vtpk(tmp_path / "a.vtpk", [_pro_fill("main.parks_v2")])
    item = _Item(
        "Vector Tile Service", saved_style={"layers": [_pro_fill("main.parks")]},
    )
    properties: dict[str, Any] = {}

    agol_sync._warn_if_saved_style_is_stale(item, vtpk, "ds_1", properties)

    assert "main.parks" in properties["_warnings"][0]
    assert "agol-sync restyle ds_1" in properties["_warnings"][0]


def test_rebuilt_package_with_same_layers_is_quiet(tmp_path: Path) -> None:
    vtpk = _write_vtpk(tmp_path / "a.vtpk", [_pro_fill()])
    for saved in (None, {"layers": [_pro_fill()]}):
        properties: dict[str, Any] = {}
        agol_sync._warn_if_saved_style_is_stale(
            _Item("Vector Tile Service", saved_style=saved), vtpk, "ds_1", properties,
        )
        assert "_warnings" not in properties
