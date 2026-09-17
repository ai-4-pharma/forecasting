"""app_ui.assumptions — Premissas e regressores usados na etapa de projecao.
(extraido de app.py single-file; split 16/09/2026).
"""

import datetime as dt
import hashlib
import json
import uuid
import polars as pl
import streamlit as st
import data_engine
import forecast_engine
from contracts import AssumptionRule, EffectType, MAX_REGRESSORS_PER_RUN, RegressorFillPolicy, RegressorSpec, ScenarioConfig, validate_assumption_rule, validate_scenario_draft
from .shell import _db

def _dimension_values() -> dict[str, list[str]]:
    """Valores reais das dimensões das entidades para seletores de filtro."""
    ent = st.session_state.dataset.entities
    if ent is None or ent.height == 0:
        return {}

    values: dict[str, set] = {}
    for row in ent.to_dicts():
        try:
            dims = json.loads(row.get("dimensions_json", "{}"))
        except Exception:  # noqa: BLE001
            dims = {}
        for k, v in dims.items():
            values.setdefault(k, set()).add(str(v))
    return {k: sorted(v) for k, v in values.items()}
def _edit_assumptions() -> None:
    """Editor de cenários e regras (P21): alvo, família, medida, filtros,
    exclusões, vigência, tipo, taxa e prioridade, alimentado por atributos
    válidos das entidades. Persiste rascunhos e preserva o cenário base."""
    st.subheader("Premissas e cenários")
    dim_vals = _dimension_values()
    if not dim_vals:
        st.caption("Dimensões indisponíveis; cadastre regras sem filtro de atributo.")

    if "scenarios" not in st.session_state or "regressors" not in st.session_state:
        if st.session_state.dataset_id:
            _sc_tmp, _rg_tmp = data_engine.load_assumptions(
                _db(), st.session_state.dataset_id
            )
            st.session_state.scenarios = (
                _sc_tmp
                if any(s.scenario_id == "base" for s in _sc_tmp)
                else [ScenarioConfig()] + _sc_tmp
            )
            st.session_state.regressors = _rg_tmp
        else:
            st.session_state.scenarios = [ScenarioConfig()]
            st.session_state.regressors = []
    scenarios: list[ScenarioConfig] = [
        s for s in st.session_state.scenarios if s.scenario_id != "base"
    ]

    # cria novo cenário
    with st.form("novo_cenario"):
        new_id = st.text_input("Novo cenário (identificador)", key="new_sc_id")
        new_name = st.text_input("Nome", key="new_sc_name")
        if st.form_submit_button("Adicionar cenário") and new_id.strip():
            if new_id.strip() == "base":
                st.error("'base' é reservado e imutável.")
            elif any(s.scenario_id == new_id for s in scenarios):
                st.warning("Cenário já existe; edite abaixo.")
            else:
                scenarios.append(
                    ScenarioConfig(
                        scenario_id=new_id.strip(),
                        name=new_name.strip() or new_id.strip(),
                        description="",
                        rules=[],
                    )
                )
                st.session_state.scenarios = [ScenarioConfig()] + scenarios

    # editar regras de cada cenário por data_editor
    removed_scenario_ids: set[str] = set()
    for sc_idx, sc in enumerate(scenarios):
        with st.expander(f"Cenário: {sc.name} ({sc.scenario_id})", expanded=True):
            if st.button("Excluir cenário", key=f"del_sc_{sc_idx}"):
                removed_scenario_ids.add(sc.scenario_id)
                continue
            rules = sc.rules
            rule_rows = []
            for r in rules:
                if not hasattr(r, "rule_id"):
                    continue
                rule_rows.append(
                    {
                        "rule_id": r.rule_id,
                        "family": r.family,
                        "target_measure": r.target_measure,
                        "effect": r.effect.value,
                        "rate": r.rate,
                        "priority": r.priority,
                        "start_period": r.start_period.isoformat()
                        if r.start_period
                        else "",
                        "end_period": r.end_period.isoformat() if r.end_period else "",
                        "filters": _filters_to_text(r.filters),
                        "excluded_entity_ids": ",".join(r.excluded_entity_ids),
                        "enabled": r.enabled,
                    }
                )
            if not rule_rows:
                st.caption("Nenhuma regra ainda. Use 'Adicionar regra' abaixo.")
            else:
                edited = st.data_editor(
                    pl.DataFrame(rule_rows), num_rows="dynamic", key=f"rules_{sc_idx}"
                )
                new_rules = []
                for row in edited.to_dicts():
                    r = _row_to_rule(sc.scenario_id, row)
                    if r is not None:
                        errs = validate_assumption_rule(r)
                        if errs:
                            st.error(f"Regra '{r.rule_id}': " + "; ".join(errs))
                        else:
                            new_rules.append(r)
                sc.rules = new_rules

            st.markdown("**Adicionar regra**")
            with st.form(f"nova_regra_{sc_idx}"):
                ffilters = {}
                if dim_vals:
                    fdim = st.selectbox(
                        "Filtrar pela dimensão", [None] + list(dim_vals)
                    )
                    if fdim:
                        fvals = st.multiselect("Valores do atributo", dim_vals[fdim])
                        ffilters = {fdim: fvals}
                c1, c2, c3 = st.columns(3)
                fam = c1.text_input("Família (ex.: preco, promocao, cmed)", "")
                tam = c2.selectbox("Medida-alvo", ["valor", "unidades"])
                eff = c3.selectbox(
                    "Efeito",
                    ["step", "pulse", "annual_step"],
                    format_func={
                        "step": "Degrau (a partir do início)",
                        "pulse": "Pulso (entre início e fim)",
                        "annual_step": "Reajuste anual recorrente",
                    }.get,
                )
                rate = st.number_input("Taxa % (ex.: 5 = +5%)", -99.0, 1000.0, 0.0)
                prio = st.number_input("Prioridade (maior vence)", 0, 100, 1)
                d1, d2 = st.columns(2)
                sp = d1.date_input("Início da vigência", dt.date.today().replace(day=1))
                ep = d2.date_input("Fim (só para pulse)", value=None)
                if st.form_submit_button("Adicionar regra"):
                    r = AssumptionRule(
                        rule_id=f"r_{uuid.uuid4().hex[:8]}",
                        scenario_id=sc.scenario_id,
                        family=fam,
                        target_measure=tam,
                        filters=ffilters,
                        start_period=sp,
                        end_period=ep,
                        effect=EffectType(eff),
                        rate=rate / 100.0,
                        priority=int(prio),
                        enabled=True,
                    )
                    errs = validate_assumption_rule(r)
                    if errs:
                        st.error("; ".join(errs))
                    else:
                        sc.rules = rules + [r]
                        st.success("Regra adicionada.")

    # validação conjunta + persistência dos rascunhos
    scenarios = [sc for sc in scenarios if sc.scenario_id not in removed_scenario_ids]
    draft_errs: list[str] = []
    for sc in scenarios:
        draft_errs.extend(validate_scenario_draft(sc))
    if "regressors" not in st.session_state:
        st.session_state.regressors = []
    _edit_regressors()
    if st.session_state.dataset_id:
        sig = hashlib.sha256(
            (
                "".join(s.to_json() for s in scenarios)
                + "".join(r.to_json() for r in st.session_state.regressors)
            ).encode()
        ).hexdigest()
        if st.session_state.get("_assump_sig") != sig:
            conn = _db()
            data_engine.save_assumptions(
                conn,
                st.session_state.dataset_id,
                scenarios + [ScenarioConfig()],
                st.session_state.regressors,
            )
            st.session_state["_assump_sig"] = sig
    st.session_state.scenarios = [ScenarioConfig()] + scenarios
    if draft_errs:
        st.warning("Ajustes pendentes: " + "; ".join(set(draft_errs)))
    else:
        st.caption(
            "Cenário 'base' é imutável. Regras são ajustes incrementais sobre a "
            "previsão base; não representam a inflação/reajuste já implícitos no "
            "histórico. CMED afeta somente Valor. MAT direto ajusta o acumulado."
        )
def _filters_to_text(filters: dict[str, list[str]]) -> str:
    return "; ".join(f"{k}={','.join(v)}" for k, v in filters.items())
def _text_to_filters(text: str) -> dict[str, list[str]]:
    """Reconstrói filtros de dimensão a partir da coluna textual do editor."""
    out: dict[str, list[str]] = {}
    if not text:
        return out
    for part in str(text).split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        vals = [x for x in v.split(",") if x]
        if vals:
            out[k.strip()] = vals
    return out
def _row_to_rule(scenario_id: str, row: dict) -> AssumptionRule | None:
    rid = str(row.get("rule_id", "")).strip()
    if not rid:
        return None
    start = (
        dt.date.fromisoformat(row["start_period"]) if row.get("start_period") else None
    )
    end = dt.date.fromisoformat(row["end_period"]) if row.get("end_period") else None
    return AssumptionRule(
        rule_id=rid,
        scenario_id=scenario_id,
        family=str(row.get("family", "")),
        target_measure=str(row.get("target_measure", "valor")),
        filters=_text_to_filters(row.get("filters", "")),
        excluded_entity_ids=[
            x for x in str(row.get("excluded_entity_ids", "")).split(",") if x
        ],
        start_period=start,
        end_period=end,
        effect=EffectType(row.get("effect", "step")),
        rate=float(row.get("rate", 0.0)),
        priority=int(row.get("priority", 0) or 0),
        enabled=bool(row.get("enabled", True)),
    )
def _forecast_periods(study, horizon: int) -> list[dt.date]:
    """Períodos futuros do horizonte, a partir do último período fechado."""
    if study.history_end is None:
        return []
    return [
        forecast_engine._add_period(study.history_end, study.model_frequency, i)
        for i in range(1, int(horizon) + 1)
    ]
def _preview_assumptions(
    scenarios: list[ScenarioConfig],
    entities,
    periods: list[dt.date],
):
    """Prévia (P22): entidades atingidas e fatores calculados por cenário."""
    preview: list[tuple] = []
    issues: list = []
    for sc in scenarios:
        if getattr(sc, "scenario_id", "base") == "base":
            continue
        rules = [r for r in sc.rules if hasattr(r, "rule_id") and r.enabled]
        if not rules:
            continue
        confl = forecast_engine.scenario_conflicts(entities, periods, rules)
        if confl:
            issues.extend(confl)
            continue
        compiled = forecast_engine.compile_assumptions(entities, periods, rules)
        if compiled.height:
            summary = compiled.group_by(["measure", "family"]).agg(
                pl.col("entity_id").n_unique().alias("n_entidades"),
                pl.col("period").n_unique().alias("n_periodos"),
            )
            preview.append((sc, compiled, summary))
    return preview, issues
def _parse_pv(text: str) -> dict[str, float]:
    """Parseia 'periodo=valor' por linha em dict período(ISO) -> float."""
    out: dict[str, float] = {}
    for line in str(text).splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, _, v = line.partition("=")
        try:
            out[k.strip()] = float(v.strip())
        except ValueError:
            continue
    return out
def _grid(pv: dict[str, float]) -> pl.DataFrame:
    if not pv:
        return pl.DataFrame({"periodo": [], "valor": []})
    return pl.DataFrame({"periodo": sorted(pv), "valor": [pv[k] for k in sorted(pv)]})
def _from_grid(df) -> dict[str, float]:
    """Converte o retorno de `st.data_editor` (polars ou pandas) em dict.

    Importante: usar o **valor retornado** pela chamada de `st.data_editor`,
    não `st.session_state[key]` — a chave de sessão de um data_editor guarda
    apenas o diff de edições (`edited_rows`/`added_rows`/`deleted_rows`), não
    a tabela completa (B2).
    """
    if hasattr(df, "to_dicts"):
        rows = df.to_dicts()
    elif hasattr(df, "to_dict"):
        rows = df.to_dict("records")
    else:
        rows = list(df)
    out: dict[str, float] = {}
    for r in rows:
        p = str(r.get("periodo", "")).strip()
        v = r.get("valor")
        if not p or v is None:
            continue
        try:
            out[p] = float(v)
        except (TypeError, ValueError):
            continue
    return out
def _repeat_last(hist: dict[str, float], n: int) -> dict[str, float]:
    """Repete o último valor histórico por n períodos futuros (comando explícito)."""
    if not hist:
        return {}
    last = max(hist.keys())
    v = hist[last]
    study = st.session_state.study
    if study is None or study.history_end is None:
        end = dt.datetime.fromisoformat(last).date()
        return {f"{(dt.date(end.year, end.month, 1))}": v}
    periods = _forecast_periods(study, n)
    return {p.isoformat(): v for p in periods}
def _edit_regressors() -> None:
    """Cadastro de regressoras (P23): grade histórica/futura, repetição explícita."""
    st.markdown("### Regressoras (efeito aprendido)")
    dim_vals = _dimension_values()
    if "regressors" not in st.session_state:
        st.session_state.regressors = []
    regs: list[RegressorSpec] = st.session_state.regressors
    st.caption(
        f"Até {MAX_REGRESSORS_PER_RUN} regressoras. Necessitam de histórico variável e "
        "cobertura futura; sem isso o candidato AutoARIMA_X fica indisponível."
    )
    with st.form("nova_regressora"):
        name = st.text_input("Nome da variável (único)", key="nr_name")
        unit = st.text_input("Unidade", key="nr_unit")
        known = st.checkbox(
            "Conhecida antecipadamente (disponível no corte)", True, key="nr_known"
        )
        fill = st.selectbox(
            "Política quando desconhecida",
            ["none", "explicit_hold"],
            format_func={
                "none": "Sem política (indisponível)",
                "explicit_hold": "Repetir último valor (explicit_hold)",
            }.get,
            key="nr_fill",
        )
        scope: dict[str, list[str]] = {}
        if dim_vals:
            sdim = st.selectbox("Escopo por dimensão", [None] + list(dim_vals))
            if sdim:
                scope = {sdim: st.multiselect("Valores", dim_vals[sdim])}
        hist_txt = st.text_area(
            "Histórico (período=valor por linha, ex.: 2026-07=1.0)", key="nr_h"
        )
        fut_txt = st.text_area("Futuro (período=valor por linha)", key="nr_f")
        if st.form_submit_button("Adicionar regressora"):
            if not name.strip():
                st.error("Informe o nome da variável.")
            elif len(regs) >= MAX_REGRESSORS_PER_RUN:
                st.error(f"Máximo de {MAX_REGRESSORS_PER_RUN} regressoras por rodada.")
            elif any(r.name == name.strip() for r in regs):
                st.warning("Regressora já existe; edite abaixo.")
            else:
                regs.append(
                    RegressorSpec(
                        regressor_id=f"reg{len(regs) + 1}",
                        name=name.strip(),
                        unit=unit,
                        scope_filters=scope,
                        known_in_advance=known,
                        fill_policy=RegressorFillPolicy(fill),
                        history_values=_parse_pv(hist_txt),
                        future_values=_parse_pv(fut_txt),
                        enabled=True,
                    )
                )
                st.session_state.regressors = regs
    for i, r in enumerate(regs):
        with st.expander(f"{r.name} (`{r.regressor_id}`)"):
            r.enabled = st.checkbox("Habilitada", value=r.enabled, key=f"ren_{i}")
            nrep = st.number_input(
                "Repetir último valor histórico por N períodos ao futuro",
                0,
                120,
                0,
                key=f"rn_{i}",
            )
            if st.button("Repetir último valor → futuro", key=f"rb_{i}"):
                r.future_values = _repeat_last(r.history_values, int(nrep or 12))
            st.caption("Grade histórico (edite as células):")
            hist_edited = st.data_editor(
                _grid(r.history_values),
                key=f"rh_{i}",
                num_rows="dynamic",
                width="stretch",
            )
            r.history_values = _from_grid(hist_edited)
            st.caption("Grade futuro (edite as células):")
            fut_edited = st.data_editor(
                _grid(r.future_values),
                key=f"rf_{i}",
                num_rows="dynamic",
                width="stretch",
            )
            r.future_values = _from_grid(fut_edited)
            st.session_state.regressors = regs
    live = [r for r in regs if r.enabled]
    if live:
        future_p = (
            _forecast_periods(st.session_state.study, 24)
            if st.session_state.study
            else []
        )
        for issue in forecast_engine.validate_regressors(live, {}, future_p):
            if issue.severity.value == "error":
                st.error(issue.message)
            else:
                st.warning(issue.message)
