"""Teste de fumaça da UI (Streamlit AppTest) — Fase 2 do _plan_opus.md.

Cobre só a casca do wizard (navegação, estado inicial); não testa lógica de
forecast, que já é coberta por `test_pipeline.py`.
"""

from __future__ import annotations

import uuid

from streamlit.testing.v1 import AppTest


def test_app_abre_sem_excecao():
    at = AppTest.from_file("app.py")
    at.run(timeout=30)
    assert not at.exception


def test_estado_inicial_etapa_arquivo_com_avancar_desabilitado():
    at = AppTest.from_file("app.py")
    at.run(timeout=30)
    assert not at.exception
    assert at.session_state["step"] == 0
    # Campo "Nome do estudo" da Etapa 1 está presente.
    assert any(
        ti.label == "Nome do estudo" for ti in at.text_input
    ), "Etapa 1 (Arquivo) não renderizou o campo 'Nome do estudo'."
    # Botão "Avançar →" existe e está desabilitado (nenhum arquivo carregado ainda).
    nav_next = [b for b in at.button if b.key == "nav_next"]
    assert len(nav_next) == 1
    assert nav_next[0].disabled is True
    # Botão "← Voltar" existe e está desabilitado na primeira etapa.
    nav_back = [b for b in at.button if b.key == "nav_back"]
    assert len(nav_back) == 1
    assert nav_back[0].disabled is True


def test_wizard_avanca_ate_qualidade_sem_navegacao_manual_pela_sidebar():
    """T2.3/T2.5: percorre Arquivo → Mapeamento → Qualidade só com upload,
    "Avançar →" e "Confirmar/Preparar" — sem tocar em roteamento de sidebar
    (que não existe mais) e verificando o avanço automático de página."""
    at = AppTest.from_file("app.py")
    at.run(timeout=30)
    assert not at.exception

    # Etapa 1 (Arquivo): nome + upload de um CSV mínimo em formato longo.
    # `_db()` usa o `.local/forecast.duckdb` real (persistente entre execuções
    # da suíte). `CanonicalDataset.fingerprint` (usado no `preparation_id`) é
    # calculado por `filename + contagens + mapping + study`, NÃO pelo
    # conteúdo dos valores — nome do estudo e do arquivo precisam variar por
    # execução, senão `save_preparation` colide com uma rodada anterior
    # (`ConstraintException: Duplicate key`; ver achado B19 registrado no
    # `_plan_opus.md`).
    run_id = uuid.uuid4().hex[:8]
    nome = [t for t in at.text_input if t.label == "Nome do estudo"][0]
    nome.set_value(f"Teste E2E wizard {run_id}").run()
    csv = "produto;periodo;unidades\nA;2024-01;10\nA;2024-02;12\nA;2024-03;9\n"
    at.file_uploader[0].upload(
        f"teste_{run_id}.csv", csv.encode("utf-8"), "text/csv"
    ).run()
    assert not at.exception
    assert at.session_state["_draft_insp"] is not None

    # "Avançar →" libera com o arquivo carregado e leva à Etapa 2 (Mapeamento).
    nav_next = [b for b in at.button if b.key == "nav_next"][0]
    assert nav_next.disabled is False
    nav_next.click().run()
    assert not at.exception
    assert at.session_state["step"] == 1

    # Confirmar mapeamento (defaults automáticos já cobrem dimensão/medida/
    # período únicos do CSV) avança sozinho para a Etapa 3 (Qualidade) — T2.5(a).
    confirmar = [b for b in at.button if "Confirmar" in (b.label or "")][0]
    confirmar.click().run()
    assert not at.exception
    assert at.session_state["step"] == 2
    assert at.session_state["dataset_id"] is not None
    assert at.session_state["_flash"] is None  # já exibido e limpo no rerun

    # Preparar dados avança sozinho para a Etapa 4 (Previsão) — T2.5(b).
    preparar = [b for b in at.button if "Preparar dados" in (b.label or "")][0]
    preparar.click().run()
    assert not at.exception
    assert at.session_state["step"] == 3
    first_prep_id = at.session_state["preparation_id"]
    assert first_prep_id is not None

    # T3.2b/B19: voltar à Qualidade e preparar de novo (mesma política) não
    # deve derrubar a UI com traceback de chave duplicada — reaproveita o
    # preparation_id já salvo.
    nav_back = [b for b in at.button if b.key == "nav_back"][0]
    nav_back.click().run()
    assert not at.exception
    assert at.session_state["step"] == 2
    preparar2 = [b for b in at.button if "Preparar dados" in (b.label or "")][0]
    preparar2.click().run()
    assert not at.exception
    assert at.session_state["step"] == 3
    assert at.session_state["preparation_id"] == first_prep_id
