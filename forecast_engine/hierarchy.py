"""Validacao, nos, agregacao e reconciliacao Bottom-Up/MinT.

Parte do pacote forecast_engine (fatiado do single-file).
"""

from __future__ import annotations

import polars as pl

from contracts import (
    HierarchyConfig,
    stable_id,
)


# ---------------------------------------------------------------------------
# Hierarquia (P25/P26)
# ---------------------------------------------------------------------------


def validate_hierarchy(hierarchy: HierarchyConfig, entities: pl.DataFrame) -> list[str]:
    """Valida partições aninhadas e chave folha única (sec 11)."""
    import json

    errs: list[str] = []
    if hierarchy is None or not hierarchy.ordered_levels:
        return errs
    levels = hierarchy.ordered_levels
    prev: set = set()
    for lv in levels:
        s = set(lv)
        if not s:
            errs.append("Nível sem dimensões.")
            continue
        if prev and not prev.issubset(s):
            errs.append(
                f"Nível {lv} não contém as dimensões do anterior ({sorted(prev)})."
            )
        prev = s
    if hierarchy.forecast_level:
        flat = [tuple(lv) for lv in levels]
        if tuple(hierarchy.forecast_level) not in flat:
            errs.append("forecast_level precisa ser um dos níveis ordenados.")
    # chave folha única (último nível identifica a entidade sem duplicidade)
    if entities is not None and entities.height:
        seen: dict[tuple, str] = {}
        for row in entities.to_dicts():
            try:
                dims = json.loads(row.get("dimensions_json", "{}"))
            except Exception:  # noqa: BLE001
                dims = {}
            key = tuple(dims.get(d, "") for d in levels[-1])
            if key in seen and seen[key] != row["entity_id"]:
                errs.append(
                    f"Folhas distintas compartilham a chave {key} no nível "
                    f"{levels[-1]}; hierarquia inválida."
                )
            seen[key] = row["entity_id"]
    return errs


def build_hierarchy_nodes(
    entities: pl.DataFrame, hierarchy: HierarchyConfig
) -> tuple[pl.DataFrame, dict[str, dict]]:
    """Constrói metadados dos nós e o mapa node->meta (sec 11).

    `ordered_levels` é a lista de níveis do topo para a folha, cada um um
    superconjunto de dimensões do anterior. Devolve um frame de nós e
    `node_meta` com `parent`, `children`, `level`, `level_name` e
    `entity_ids` (entidades-folha sob o nó).
    """
    import json

    levels = hierarchy.ordered_levels
    leaf_level = len(levels) - 1
    node_meta: dict[str, dict] = {}
    for row in entities.to_dicts():
        try:
            dims = json.loads(row.get("dimensions_json", "{}"))
        except Exception:  # noqa: BLE001
            dims = {}
        eid = row["entity_id"]
        prev_node: str | None = None
        for idx, lv in enumerate(levels):
            key = tuple(dims.get(d, "") for d in lv)
            nid = stable_id("h", idx, *key)
            meta = node_meta.get(nid)
            if meta is None:
                meta = {
                    "parent": None,
                    "children": set(),
                    "level": idx,
                    "level_name": " > ".join(str(v) for v in key) if key else "total",
                    "entity_ids": set(),
                    "node_id": nid,
                }
                node_meta[nid] = meta
            meta["entity_ids"].add(eid)
            if prev_node is not None:
                node_meta[prev_node]["children"].add(nid)
                meta["parent"] = prev_node
            prev_node = nid
    node_rows = [
        {
            "node_id": meta["node_id"],
            "level": meta["level"],
            "level_name": meta["level_name"],
            "parent_node_id": meta["parent"],
            "entity_ids": sorted(meta["entity_ids"]),
            "n_entities": len(meta["entity_ids"]),
        }
        for meta in node_meta.values()
    ]
    # enraizar nós do nível 0 sob um único total, se solicitado
    if hierarchy.include_total:
        total_id = stable_id("h", "total")
        node_meta.setdefault(
            total_id,
            {
                "parent": None,
                "children": set(nid for nid, m in node_meta.items() if m["level"] == 0),
                "level": -1,
                "level_name": "Total",
                "entity_ids": set(),
                "node_id": total_id,
            },
        )
        for nid, m in node_meta.items():
            if m["level"] == 0:
                m["parent"] = total_id
    # garantir order determinística
    if node_rows and "node_id" in node_rows[0]:
        nf = pl.DataFrame(node_rows)
    else:
        nf = pl.DataFrame(
            {
                "node_id": [],
                "level": [],
                "level_name": [],
                "parent_node_id": [],
                "entity_ids": [],
                "n_entities": [],
            }
        )
    return nf, node_meta


def _entity_to_leaf(node_meta: dict[str, dict]) -> dict[str, str]:
    """entity_id -> node de folha (nível máximo) que a contém."""
    leaf_level = max((m["level"] for m in node_meta.values()), default=0)
    out: dict[str, str] = {}
    for nid, meta in node_meta.items():
        if meta["level"] != leaf_level:
            continue
        for eid in meta["entity_ids"]:
            out[eid] = nid
    return out


def _entity_dimensions_json(entities: pl.DataFrame | None, entity_id: str) -> str:
    """Retorna `dimensions_json` de uma entidade (ou '{}' se ausente)."""
    import json as _json

    if entities is None or entities.height == 0:
        return "{}"
    row = entities.filter(pl.col("entity_id") == entity_id)
    if row.height == 0 or "dimensions_json" not in row.columns:
        return "{}"
    return str(row["dimensions_json"].first() or "{}")


def build_run_nodes(
    prepared: pl.DataFrame,
    entities: pl.DataFrame | None,
    hierarchy: HierarchyConfig | None,
    node_meta: dict[str, dict] | None,
) -> pl.DataFrame:
    """Frame de nós da rodada para persistência em `run_nodes` (sec 8.1/11).

    Inclui as séries preditas (nível `folha`, entity_id real) e os nós
    hierárquicos do `node_meta` (nível `level_name`, com `coverage_json`
    listando as entidades-folha sob o nó). Serve ao dashboard para filtrar
    por nível/dimensão e reconstruir histórico de nós agregados sem importar
    o motor de previsão em data_engine (arquitetura sec 3.2).
    """
    import json as _json

    nodes: dict[str, dict] = {}
    series_rows = prepared.group_by("series_id").agg(
        pl.col("entity_id").first().alias("entity_id")
    )
    for r in series_rows.to_dicts():
        sid = str(r["series_id"])
        eid = str(r["entity_id"])
        nodes[sid] = {
            "node_id": sid,
            "level": "folha",
            "parent_node_id": None,
            "entity_id": eid,
            "dimensions_json": _entity_dimensions_json(entities, eid),
            "coverage_json": _json.dumps(
                {"entity_ids": [eid], "n_entities": 1}, ensure_ascii=False
            ),
        }
    if node_meta:
        for nid, meta in node_meta.items():
            nid = str(nid)
            if nid in nodes:
                continue
            eids = sorted(str(e) for e in (meta.get("entity_ids") or set()))
            nodes[nid] = {
                "node_id": nid,
                "level": str(meta.get("level_name", "total")),
                "parent_node_id": (
                    str(meta["parent"]) if meta.get("parent") is not None else None
                ),
                "entity_id": eids[0] if len(eids) == 1 else None,
                "dimensions_json": (
                    _entity_dimensions_json(entities, eids[0])
                    if len(eids) == 1
                    else "{}"
                ),
                "coverage_json": _json.dumps(
                    {"entity_ids": eids, "n_entities": len(eids)}, ensure_ascii=False
                ),
            }
    rows = [nodes[nid] for nid in sorted(nodes)]
    if not rows:
        return pl.DataFrame(
            {
                "node_id": [],
                "level": [],
                "parent_node_id": [],
                "entity_id": [],
                "dimensions_json": [],
                "coverage_json": [],
            }
        )
    return pl.DataFrame(rows)


def aggregate_history(
    prepared: pl.DataFrame,
    entities: pl.DataFrame,
    hierarchy: HierarchyConfig,
    level: int,
) -> pl.DataFrame:
    """Agrega histórico preparado por nó de um nível, para previsão independente.

    Folhas exibidas somam seus valores observados preparados; períodos com
    filhos ausentes ficam com `observed=False` para o nó (não avaliados como
    total completo). Usa `entity_id` de cada observação para localizar o nó.
    """
    import json

    levels = hierarchy.ordered_levels
    node_of_entity: dict[str, str] = {}
    for row in entities.to_dicts():
        try:
            dims = json.loads(row.get("dimensions_json", "{}"))
        except Exception:  # noqa: BLE001
            dims = {}
        key = tuple(dims.get(d, "") for d in levels[level])
        node_of_entity[row["entity_id"]] = stable_id("h", level, *key)
    df = prepared.with_columns(
        pl.col("entity_id")
        .map_elements(lambda e: node_of_entity.get(e, e), return_dtype=pl.String)
        .alias("node_id")
    ).with_columns(
        pl.col("node_id").alias("series_id"),
        pl.col("node_id").alias("entity_id"),
    )
    out = (
        df.group_by(["node_id", "series_id", "entity_id", "ds", "measure"])
        .agg(
            pl.col("y").sum().alias("y"),
            (pl.col("observed").all()).alias("observed"),
        )
        .with_columns(pl.lit(level).alias("level"))
    )
    return out.sort(["series_id", "ds"])


def reconcile_bottom_up(
    predictions: pl.DataFrame,
    hierarchy: HierarchyConfig | None,
    node_meta: dict[str, dict] | None = None,
) -> pl.DataFrame:
    """Soma folhas limitadas a zero para os nós pais (Bottom-Up), por cenário/medida.

    Multi-nível: agrega do nível mais profundo ao topo. `node_meta` mapeia
    node_id -> {"parent", "level", "level_name", ...}. Cobertura incompleta
    gera `partial_coverage`; limites marginais nunca são somados.
    """
    if predictions.height == 0:
        return predictions
    base = predictions
    if hierarchy is None or not node_meta:
        return base.with_columns(pl.lit(None, dtype=pl.Float64).alias("_agg_unused"))

    leaf_of = _entity_to_leaf(node_meta)
    parent_by_node = {k: v.get("parent") for k, v in node_meta.items()}
    children_by_parent: dict[str, list[str]] = {}
    for node, parent in parent_by_node.items():
        if parent:
            children_by_parent.setdefault(parent, []).append(node)
    if not children_by_parent:
        return base.with_columns(pl.lit(None, dtype=pl.Float64).alias("_agg_unused"))

    # camada 1: linhas de folha indexadas por nó de folha
    rows = base.to_dicts()
    leaf_rows: dict[tuple, list[dict]] = {}
    for r in rows:
        leaf = leaf_of.get(str(r["entity_id"]), str(r["node_id"]))
        key = (leaf, r["scenario_id"], r["measure"], r["ds"])
        leaf_rows.setdefault(key, []).append(r)

    agg_rows = list(rows)
    # a cada rodada, agrega o nível atual da fronteira nos seus pais
    frontier: dict[tuple, dict] = {}
    for (leaf, scenario, measure, ds), group in leaf_rows.items():
        bucket = frontier.setdefault(
            (leaf, scenario, measure, ds), {"sum": 0.0, "count": 0}
        )
        for r in group:
            if r["yhat"] is not None:
                bucket["sum"] += float(r["yhat"])
            bucket["count"] += 1
    while True:
        parents_map: dict[tuple, dict] = {}
        for (node, scenario, measure, ds), bucket in frontier.items():
            parent = parent_by_node.get(node)
            if parent is None:
                continue
            nk = (parent, scenario, measure, ds)
            nb = parents_map.setdefault(nk, {"sum": 0.0, "children": set()})
            nb["sum"] += bucket["sum"]
            nb["children"].add(node)
        if not parents_map:
            break
        for (node, scenario, measure, ds), nb in parents_map.items():
            children = children_by_parent.get(node, [])
            agg_rows.append(
                {
                    "node_id": node,
                    "entity_id": node,
                    "level": node_meta.get(node, {}).get("level_name", "total"),
                    "measure": measure,
                    "scenario_id": scenario,
                    "ds": ds,
                    "yhat": nb["sum"],
                    "lo80": None,
                    "hi80": None,
                    "model_alias": "BottomUp",
                    "interval_method": "unavailable_insufficient_history",
                    "status": (
                        "ok" if nb["children"] == set(children) else "partial_coverage"
                    ),
                }
            )
        frontier = parents_map

    moves = pl.DataFrame(agg_rows)
    moves = moves.with_columns(
        pl.when(pl.col("yhat").is_null())
        .then(pl.col("yhat"))
        .otherwise(pl.col("yhat").clip(lower_bound=0.0))
        .alias("yhat")
    )
    return moves.drop([c for c in moves.columns if c == "_agg_unused"])

