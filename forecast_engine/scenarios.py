"""Premissas, cenarios deterministas e MAT derivado.

Parte do pacote forecast_engine (fatiado do single-file).
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any
import polars as pl

from contracts import (
    EffectType,
    ScenarioConfig,
    AssumptionRule,
    RegressorSpec,
    ValidationIssue,
    Severity,
    ERR_SCENARIO_PRIORITY_TIE,
    ERR_SCENARIO_PARTIAL_NODE,
)

from .regressors import _regressor_family_conflicts
from .hierarchy import _entity_to_leaf


# ---------------------------------------------------------------------------
# Premissas e cenários (P22)
# ---------------------------------------------------------------------------


def _rule_applies(rule: AssumptionRule, e: dict, p: dt.date) -> bool:
    """Verifica se a regra produz fator para a entidade/período (sec 10.1)."""
    if not rule.enabled:
        return False
    if rule.rate <= -1.0:
        return False
    if rule.start_period is None or p < rule.start_period:
        return False
    if rule.effect != EffectType.STEP and rule.end_period and p > rule.end_period:
        return False
    if e["entity_id"] in rule.excluded_entity_ids:
        return False
    return _entity_matches(e, rule.filters)


def compile_assumptions(
    entities: pl.DataFrame, periods: list[dt.date], rules: list[AssumptionRule]
) -> pl.DataFrame:
    """Expande regras por entidade/período em fatores, resolvendo prioridade."""
    ent = entities.to_dicts()
    rows: list[dict] = []
    for rule in rules:
        if not rule.enabled:
            continue
        for e in ent:
            for p in periods:
                if not _rule_applies(rule, e, p):
                    continue
                rows.append(
                    {
                        "entity_id": e["entity_id"],
                        "period": p,
                        "rule_id": rule.rule_id,
                        "family": rule.family,
                        "measure": rule.target_measure,
                        "factor": _factor_for(rule, p),
                        "priority": rule.priority,
                        "rule_effect": rule.effect.value,
                    }
                )
    if not rows:
        return pl.DataFrame(
            {
                "entity_id": [],
                "period": [],
                "rule_id": [],
                "family": [],
                "measure": [],
                "factor": [],
                "priority": [],
                "rule_effect": [],
            },
            schema={
                "entity_id": pl.String,
                "period": pl.Date,
                "rule_id": pl.String,
                "family": pl.String,
                "measure": pl.String,
                "factor": pl.Float64,
                "priority": pl.Int64,
                "rule_effect": pl.String,
            },
        )
    # resolução de prioridade dentro da mesma família/entidade/medida/período
    df = pl.DataFrame(rows).sort(
        ["entity_id", "period", "family", "measure", "priority"]
    )
    dedup = df.unique(subset=["entity_id", "period", "family", "measure"], keep="last")
    return dedup


def scenario_conflicts(
    entities: pl.DataFrame, periods: list[dt.date], rules: list[AssumptionRule]
) -> list[ValidationIssue]:
    """Detecta empate de prioridade em regras sobrepostas (sec 10.1).

    Dentro da mesma família/entidade/medida/período, empate no maior priority
    bloqueia o cenário; se o maior priority for único, ele vence sem conflito.
    """
    issues: list[ValidationIssue] = []
    buckets: dict[tuple, list[tuple[int, str]]] = defaultdict(list)
    for rule in rules:
        if not rule.enabled:
            continue
        for e in entities.to_dicts():
            for p in periods:
                if not _rule_applies(rule, e, p):
                    continue
                key = (e["entity_id"], p, rule.family, rule.target_measure)
                buckets[key].append((rule.priority, rule.rule_id))
    for (eid, p, family, measure), items in buckets.items():
        by_prio = sorted(items, key=lambda t: -t[0])
        max_prio = by_prio[0][0]
        tied = [t for t in by_prio if t[0] == max_prio]
        if len(tied) >= 2:
            issues.append(
                ValidationIssue(
                    code=ERR_SCENARIO_PRIORITY_TIE,
                    severity=Severity.ERROR,
                    message=(
                        f"Empate de prioridade {max_prio} na família '{family}' "
                        f"para a entidade {eid} em {p.isoformat()}; o cenário "
                        "é bloqueado."
                    ),
                    entity_ids=[eid],
                    details={
                        "family": family,
                        "measure": measure,
                        "period": p.isoformat(),
                        "rule_ids": [t[1] for t in tied],
                    },
                )
            )
    return issues


def _annual_occurrences(rule: AssumptionRule, p: dt.date) -> int:
    """Número de aniversários anuais do início até p, inclusive (sec 10.1)."""
    if rule.effect != EffectType.ANNUAL_STEP or rule.start_period is None:
        return 0
    if p < rule.start_period:
        return 0
    s = rule.start_period
    if p.day != s.day or p.month != s.month:
        # datas do grid são o primeiro dia do período; o aniversário ocorre no
        # mesmo mês/trimestre do início, na mesma posição do calendário.
        anniv = (s.month, s.day)
    else:
        anniv = (s.month, s.day)
    years = p.year - s.year
    reached = (p.month, p.day) >= anniv
    return years + (1 if reached else 0)


def _factor_for(rule: AssumptionRule, p: dt.date) -> float:
    if rule.effect == EffectType.ANNUAL_STEP:
        k = _annual_occurrences(rule, p)
        return (1 + rule.rate) ** k if k > 0 else 1.0
    return 1 + rule.rate


def _entity_matches(e: dict, filters: dict[str, list[str]]) -> bool:
    if not filters:
        return True
    import json

    try:
        dims = json.loads(e.get("dimensions_json", "{}"))
    except Exception:  # noqa: BLE001
        dims = {}
    for k, vals in filters.items():
        if dims.get(k, "") not in vals:
            return False
    return True


def apply_scenario(
    predictions: pl.DataFrame, compiled_rules: pl.DataFrame
) -> pl.DataFrame:
    """Multiplica previsões por fatores das regras (yhat_scenario = yhat_base × fator).

    Regras de famílias diferentes se compõem multiplicativamente (sec 10.1);
    fatores de uma mesma família já foram resolvidos por prioridade em
    compile_assumptions. Aplica piso zero e limita limites, quando presentes.
    """
    if compiled_rules.height == 0 or predictions.height == 0:
        return predictions
    fact = (
        compiled_rules.rename({"period": "ds"})
        .select(["entity_id", "ds", "measure", "factor"])
        .group_by(["entity_id", "ds", "measure"])
        .agg(pl.col("factor").product().alias("factor"))
        .with_columns(pl.col("factor").fill_null(1.0))
    )
    out = predictions.join(fact, on=["entity_id", "ds", "measure"], how="left")
    out = out.with_columns(
        pl.when(pl.col("factor").is_null())
        .then(pl.lit(1.0))
        .otherwise(pl.col("factor"))
        .alias("factor")
    )
    out = out.with_columns(
        (out["yhat"] * out["factor"]).clip(lower_bound=0.0).alias("yhat"),
        pl.when(out["lo80"].is_null())
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise((out["lo80"] * out["factor"]).clip(lower_bound=0.0))
        .alias("lo80"),
        pl.when(out["hi80"].is_null())
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise((out["hi80"] * out["factor"]).clip(lower_bound=0.0))
        .alias("hi80"),
    )
    return out.drop([c for c in ("factor",) if c in out.columns])


def apply_scenario_nodes(
    predictions: pl.DataFrame,
    compiled: pl.DataFrame,
    node_meta: dict[str, dict],
) -> tuple[pl.DataFrame, list[ValidationIssue]]:
    """Aplica fatores por nó em previsão independente agregada (sec 11).

    Se todas as entidades-folha de um nó compartilham o mesmo fator, o nó é
    multiplicado por ele; se nenhuma folha é afetada, fator 1; se apenas parte
    das folhas for afetada, a aplicação parcial é bloqueada (linha removida e
    `ValidationIssue` emitido) — regra parcial nunca multiplica o nó inteiro.
    """
    if compiled.height == 0 or predictions.height == 0:
        return predictions, []
    leaf_of = _entity_to_leaf(node_meta)
    # conjunto de nós-folha sob cada nó (qualquer nível que o projete)
    node_leaves: dict[str, set] = {
        str(nid): set(
            leaf_of.get(str(eid), str(eid)) for eid in meta.get("entity_ids", set())
        )
        for nid, meta in node_meta.items()
    }
    fact: dict[tuple, float] = {}
    for r in compiled.to_dicts():
        leaf = leaf_of.get(r["entity_id"])
        if leaf is None:
            leaf = r["entity_id"]
        fact[(leaf, r["period"], r["measure"])] = r["factor"]
    rows_out: list[dict] = []
    issues: list[ValidationIssue] = []
    for r in predictions.to_dicts():
        node_id = str(r["node_id"])
        leaves = node_leaves.get(
            node_id, {str(r["entity_id"])} if r["entity_id"] else {node_id}
        )
        ds = r["ds"]
        measure = r["measure"]
        affected = [
            (l, fact[(l, ds, measure)]) for l in leaves if (l, ds, measure) in fact
        ]
        leaves_affected = {l for l, _ in affected}
        leaves_unaffected = set(leaves) - leaves_affected
        if not affected:
            factor = 1.0
        elif not leaves_unaffected and len({f for _, f in affected}) == 1:
            factor = affected[0][1]
        else:
            issues.append(
                ValidationIssue(
                    code=ERR_SCENARIO_PARTIAL_NODE,
                    severity=Severity.ERROR,
                    message=(
                        f"Premissa parcial no nó independente '{node_id}': "
                        "a regra não cobre todas as folhas; aplicação bloqueada."
                    ),
                    entity_ids=[node_id],
                )
            )
            continue
        nr = dict(r)
        nr["yhat"] = max(0.0, float(r["yhat"]) * factor)
        if nr.get("lo80") is not None:
            nr["lo80"] = max(0.0, nr["lo80"] * factor)
        if nr.get("hi80") is not None:
            nr["hi80"] = max(0.0, nr["hi80"] * factor)
        rows_out.append(nr)
    out = (
        pl.DataFrame(rows_out, schema=predictions.schema)
        if rows_out
        else pl.DataFrame([], schema=predictions.schema)
    )
    return out, issues


def generate_scenario_predictions(
    base_predictions: pl.DataFrame,
    scenarios: list[ScenarioConfig],
    entities: pl.DataFrame | None,
    recompute_base: Any = None,
    regressors: list[RegressorSpec] | None = None,
    node_meta: dict[str, dict] | None = None,
) -> tuple[pl.DataFrame, list[ValidationIssue], pl.DataFrame]:
    """Gera previsões por cenário determinístico a partir da base (P22).

    Devolve (scenario_predictions, issues, factors_trace). O cenário base é
    preservado. Cenários sem regras replicam a base sob outro scenario_id;
    conflitos de prioridade bloqueiam o cenário com ValidationIssue. O rastro
    contém, por série/período, as regras aplicadas e o fator resultante.

    `recompute_base(scenario)` (quando informado) devolve a previsão base
    recalculada para cenários com `regressor_future_overrides` (P24); sem ela,
    o override de regressora é ignorado neste estágio. Uma família usada ao
    mesmo tempo como regressora (override) e multiplicador (regra) bloqueia o
    cenário (sec 10.2). Com `node_meta` informado (independente agregado), as
    regras são aplicadas por nó com bloqueio de escopo parcial (sec 11).
    """
    issues: list[ValidationIssue] = []
    trace_rows: list[dict] = []
    if base_predictions.height == 0 or entities is None or entities.height == 0:
        return (
            pl.DataFrame([], schema=base_predictions.schema),
            issues,
            pl.DataFrame(),
        )
    periods = sorted(set(base_predictions["ds"].to_list()))
    reg_name_by_id = {r.regressor_id: r.name for r in (regressors or [])}
    scenario_rows: list[dict] = []
    added: set[tuple] = set()
    for sc in scenarios:
        if sc.scenario_id == "base":
            continue
        rules = [r for r in sc.rules if hasattr(r, "rule_id") and r.enabled]
        if sc.regressor_future_overrides:
            fam_conflicts = _regressor_family_conflicts(sc, reg_name_by_id)
            if fam_conflicts:
                issues.extend(fam_conflicts)
                continue
        base_for_sc = base_predictions
        if sc.regressor_future_overrides and recompute_base is not None:
            base_for_sc = recompute_base(sc)
        if not rules:
            for r in base_for_sc.to_dicts():
                key = (r["node_id"], r["measure"], sc.scenario_id, r["ds"])
                if key in added:
                    continue
                added.add(key)
                scenario_rows.append(dict(r, scenario_id=sc.scenario_id))
            continue
        confl = scenario_conflicts(entities, periods, rules)
        if confl:
            issues.extend(confl)
            continue
        compiled = compile_assumptions(entities, periods, rules)
        if compiled.height:
            trace_rows.extend(compiled.to_dicts())
        if node_meta is not None:
            applied, node_issues = apply_scenario_nodes(
                base_for_sc, compiled, node_meta
            )
            if node_issues:
                issues.extend(node_issues)
                continue
        else:
            applied = apply_scenario(base_for_sc, compiled)
        applied = applied.with_columns(
            pl.lit(sc.scenario_id, dtype=pl.String).alias("scenario_id")
        )
        for r in applied.to_dicts():
            key = (r["node_id"], r["measure"], sc.scenario_id, r["ds"])
            if key in added:
                continue
            added.add(key)
            scenario_rows.append(r)
    scenario_df = (
        pl.DataFrame(scenario_rows, schema=base_predictions.schema)
        if scenario_rows
        else pl.DataFrame([], schema=base_predictions.schema)
    )
    trace_df = pl.DataFrame(trace_rows) if trace_rows else pl.DataFrame()
    return scenario_df, issues, trace_df


def derive_mat_derived(
    history: pl.DataFrame,
    predictions: pl.DataFrame,
    n_lag: int = 12,
) -> pl.DataFrame:
    """Deriva visão MAT (acumulado de n meses encerrado em cada t) da série mensal.

    Combina histórico preparado e previsão da mesma série/medida/cenário,
    aplica piso zero nos componentes derivados e só emite posição quando os
    n componentes existirem (sec 5.3). Não soma limites de intervalos.
    """
    rows: list[dict] = []
    if predictions.height == 0:
        return pl.DataFrame(
            {
                "node_id": [],
                "measure": [],
                "scenario_id": [],
                "ds": [],
                "mat": [],
            },
            schema={
                "node_id": pl.String,
                "measure": pl.String,
                "scenario_id": pl.String,
                "ds": pl.Date,
                "mat": pl.Float64,
            },
        )
    hist = history.rename({"series_id": "node_id"})[["node_id", "ds", "measure", "y"]]
    for (node_id, measure, scenario_id), gp in predictions.group_by(
        ["node_id", "measure", "scenario_id"]
    ):
        h = hist.filter(
            pl.col("node_id") == node_id, pl.col("measure") == measure
        ).select(["ds", "y"])
        fc = gp.select(["ds", "yhat"]).rename({"yhat": "y"})
        combined = pl.concat([h, fc]).sort("ds")
        combined = combined.with_columns(
            pl.col("y").clip(lower_bound=0.0).alias("y_floor")
        )
        combined = combined.with_columns(
            pl.col("y_floor")
            .rolling_sum(window_size=n_lag, min_samples=n_lag)
            .alias("mat_sum")
        )
        future_dates = set(gp["ds"].to_list())
        for r in combined.filter(pl.col("ds").is_in(future_dates)).to_dicts():
            if r["mat_sum"] is None:
                continue
            rows.append(
                {
                    "node_id": node_id,
                    "measure": measure,
                    "scenario_id": scenario_id,
                    "ds": r["ds"],
                    "mat": float(r["mat_sum"]),
                }
            )
    if not rows:
        return pl.DataFrame(
            {
                "node_id": [],
                "measure": [],
                "scenario_id": [],
                "ds": [],
                "mat": [],
            },
            schema={
                "node_id": pl.String,
                "measure": pl.String,
                "scenario_id": pl.String,
                "ds": pl.Date,
                "mat": pl.Float64,
            },
        )
    return pl.DataFrame(rows)

