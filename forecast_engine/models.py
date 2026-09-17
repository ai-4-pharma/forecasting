"""Registro de candidatos, seletor SFE e fabrica StatsForecast.

Parte do pacote forecast_engine (fatiado do single-file).
"""

from __future__ import annotations

from contracts import (
    ForecastConfig,
    ModelSpec,
    SourceFrequency,
    MIN_INTERMITTENT_PERIODS,
)

from .const import MODEL_LABELS_PT, RANK_BY_ALIAS


def model_label(alias: str) -> str:
    return MODEL_LABELS_PT.get(alias, alias)


def _make_spec(alias: str, **kw) -> ModelSpec:
    base = {
        "alias": alias,
        "params": {},
        "supports_regressors": False,
        "supports_intervals": False,
        "min_train": 3,
        "min_seasonal_cycles": 2,
        "intermittent": False,
        "requires_positives": False,
        "baseline": False,
        "global_model": False,
        "need_ml": False,
        "rank": RANK_BY_ALIAS.get(alias, 100),
    }
    base.update(kw)
    return ModelSpec(**base)


def build_candidates(profile, study, config: ForecastConfig) -> list[ModelSpec]:
    """Constrói lista de ModelSpec conforme o perfil/modo + seleção do usuário.

    Se `config.candidate_aliases` estiver preenchido (seletor da UI), apenas
    esses aliases concorrem — modo comparador, sem decisão automática oculta.
    """
    interval_req = config.interval_level if config.interval_level else None
    candidates: list[ModelSpec] = []
    wanted = set(getattr(config, "candidate_aliases", []) or [])

    def add(alias: str, **kw) -> None:
        if wanted and alias not in wanted:
            return
        s = _make_spec(alias, **kw)
        candidates.append(s)

    add("Naive", baseline=True, min_train=1)
    add("MediaMovel3", baseline=True, min_train=3, params={"window": 3})
    add("MediaMovel6", baseline=True, min_train=6, params={"window": 6})
    add("MediaMovel12", baseline=True, min_train=12, params={"window": 12})
    add("HistoricAverage", baseline=True, min_train=3)
    if interval_req and candidates and candidates[-1].alias == "HistoricAverage":
        candidates[-1].supports_intervals = True

    if study.source_frequency in (SourceFrequency.MONTHLY, SourceFrequency.QUARTERLY):
        add("SeasonalNaive", baseline=True, min_train=8, min_seasonal_cycles=2)

    add("RegLinearDrift", baseline=True, min_train=4)
    add("Holt", min_train=8, supports_intervals=True)
    add("HoltDamped", min_train=8, supports_intervals=True)

    if config.mode == config.mode.ADVANCED:
        add("AutoCES", min_train=8, supports_intervals=True)
        add("AutoARIMA", min_train=8, supports_intervals=True)
        add("AutoTBATS", min_train=24, supports_intervals=False)

    if config and config.regressor_ids:
        add(
            "AutoARIMA_X",
            min_train=8,
            supports_intervals=True,
            supports_regressors=True,
        )

    add("AutoETS", min_train=8, supports_intervals=True)
    add("ETS_Damped", min_train=8, supports_intervals=True)
    add("AutoTheta", min_train=8, supports_intervals=True)
    add(
        "CrostonSBA",
        intermittent=True,
        min_train=MIN_INTERMITTENT_PERIODS,
        requires_positives=True,
        min_seasonal_cycles=1,
    )
    add(
        "TSB",
        intermittent=True,
        min_train=MIN_INTERMITTENT_PERIODS,
        requires_positives=True,
        min_seasonal_cycles=1,
        params={"alpha_d": 0.2, "alpha_p": 0.2},
    )
    return candidates


def _extract_yhat(row: dict, alias: str) -> float:
    """Extrai a previsão pontual da linha devolvida pelo StatsForecast."""
    for key in (alias, "AutoETS", "unique_id"):
        v = row.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    # lo-80/hi-80 presentes; yhat é fianl a primeira chave não-lo/hi
    for k, v in row.items():
        if k not in ("ds", "lo-80", "hi-80", "unique_id") and isinstance(
            v, (int, float)
        ):
            return float(v)
    return 0.0


def _sf_n_jobs(config=None) -> int:
    """Paralelismo do StatsForecast entre séries. Respeita `config.n_jobs`
    (default 1; no Windows, pool de processos é caro e mais lento)."""
    import os as _os

    try:
        cfg_n = int(getattr(config, "n_jobs", 0) or 0)
    except Exception:  # noqa: BLE001
        cfg_n = 0
    if cfg_n >= 1:
        return cfg_n
    return max(1, min(8, _os.cpu_count() or 4))


def _build_sf_model(alias: str, season_len: int):
    """Fábrica única de modelos StatsForecast (CV e final usam a mesma)."""
    from statsforecast.models import (
        Naive,
        HistoricAverage,
        SeasonalNaive,
        AutoETS,
        AutoTheta,
        CrostonSBA,
        TSB,
        AutoCES,
        AutoARIMA,
        AutoTBATS,
        WindowAverage,
        RandomWalkWithDrift,
        Holt,
    )

    if alias == "SeasonalNaive":
        return SeasonalNaive(season_length=season_len)
    if alias == "AutoARIMA":
        return AutoARIMA(
            season_length=season_len,
            max_p=3,
            max_q=3,
            max_P=1,
            max_Q=1,
            max_order=5,
            approximation=True,
        )
    if alias == "AutoTBATS":
        return AutoTBATS(
            season_length=season_len,
            use_boxcox=False,
            use_arma_errors=False,
        )
    table = {
        "Naive": Naive,
        "MediaMovel3": lambda: WindowAverage(window_size=3),
        "MediaMovel6": lambda: WindowAverage(window_size=6),
        "MediaMovel12": lambda: WindowAverage(window_size=12),
        "HistoricAverage": HistoricAverage,
        "RegLinearDrift": RandomWalkWithDrift,
        # Holt/HoltDamped são de tendência: permanecem não-sazonais (ciclo 1).
        "Holt": lambda: Holt(season_length=1),
        "HoltDamped": lambda: AutoETS(model="AAdN", damped=True),
        # CrostonSBA/TSB não aceitam season_length (intermitentes, sem ciclo).
        "CrostonSBA": CrostonSBA,
        "AutoETS": lambda: AutoETS(season_length=season_len),
        "ETS_Damped": lambda: AutoETS(season_length=season_len, damped=True),
        "AutoTheta": lambda: AutoTheta(season_length=season_len),
        "AutoCES": lambda: AutoCES(season_length=season_len),
    }
    if alias == "TSB":
        return TSB(alpha_d=0.2, alpha_p=0.2)
    factory = table.get(alias)
    if factory is None:
        return None
    return factory() if callable(factory) else factory
