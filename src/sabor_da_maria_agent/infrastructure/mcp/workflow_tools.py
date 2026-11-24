"""Official conversational workflow tools (thin MCP transport adapters).

All business rules live in ``PlanningService`` and the domain. These
functions only build the service, pass arguments through and serialize the
result. State is persisted in SQLite (see ``SABOR_DA_MARIA_DB_PATH``).
"""

from collections.abc import Callable
from typing import Any, Literal

from fastmcp import FastMCP

from sabor_da_maria_agent.application.planning import (
    PlanningService,
    PlanningSession,
    RecipeCandidate,
    WorkflowEvent,
)
from sabor_da_maria_agent.domain.pantry import PantryCatalog
from sabor_da_maria_agent.domain.workflow import CandidateStatus
from sabor_da_maria_agent.infrastructure.persistence.sqlite import (
    SQLitePlanningRepository,
    default_db_path,
)

_NEXT_ACTION: dict[CandidateStatus, str] = {
    CandidateStatus.DISCOVERED: "Call evaluate_recipe_candidate.",
    CandidateStatus.NEEDS_INFORMATION: (
        "Ask Dona Maria for the missing_information items, then call "
        "update_kitchen_profile or revise the candidate with "
        "register_recipe_candidate, and evaluate again. Never assume answers."
    ),
    CandidateStatus.EVALUATED: "Internal state; evaluation is in progress.",
    CandidateStatus.INFEASIBLE: (
        "Explain the blockers. Do not recommend purchases for this recipe."
    ),
    CandidateStatus.FEASIBLE: "Internal state; awaiting approval request.",
    CandidateStatus.AWAITING_APPROVAL: (
        "Present pantry comparison, purchases and remaining budget, ask Dona "
        "Maria for an explicit decision, then call approve_recipe_candidate "
        "with this candidate_version."
    ),
    CandidateStatus.APPROVED: "Call calculate_approved_recipe with this version.",
    CandidateStatus.REJECTED: "Recipe rejected. Offer other candidates.",
    CandidateStatus.COSTED: "Internal state; pricing in progress.",
    CandidateStatus.PRICED: (
        "Present CMV and the per-portion scenarios; Dona Maria chooses the price."
    ),
}


def _session_payload(session: PlanningSession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "session_version": session.version,
        "kitchen_profile": (
            session.kitchen_profile.model_dump(mode="json")
            if session.kitchen_profile is not None
            else None
        ),
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
    }


def _candidate_payload(candidate: RecipeCandidate) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "session_id": candidate.session_id,
        "candidate_version": candidate.version,
        "status": candidate.status.value,
        "next_action": _NEXT_ACTION[candidate.status],
        "recipe": candidate.recipe.model_dump(mode="json"),
        "source_evidence": [
            item.model_dump(mode="json") for item in candidate.evidence
        ],
        "supplemental_quotes": [q.model_dump(mode="json") for q in candidate.quotes],
        "allow_partial_cmv": candidate.allow_partial_cmv,
        "production_confirmation": (
            candidate.production_confirmation.model_dump(mode="json")
            if candidate.production_confirmation is not None
            else None
        ),
        "evaluation": (
            candidate.evaluation.model_dump(mode="json")
            if candidate.evaluation is not None
            else None
        ),
        "approval": (
            candidate.approval.model_dump(mode="json")
            if candidate.approval is not None
            else None
        ),
        "calculation": candidate.calculation,
    }


def _event_payload(event: WorkflowEvent) -> dict[str, Any]:
    return {
        "candidate_id": event.candidate_id,
        "event_type": event.event_type,
        "from_status": event.from_status.value if event.from_status else None,
        "to_status": event.to_status.value if event.to_status else None,
        "version": event.version,
        "payload": event.payload,
        "created_at": event.created_at.isoformat(),
    }


def register_workflow_tools(
    mcp: FastMCP,
    catalog_loader: Callable[[], PantryCatalog],
) -> None:
    def service() -> PlanningService:
        # Resolved per call so the configured database path is always honoured.
        return PlanningService(
            SQLitePlanningRepository(default_db_path()), catalog_loader
        )

    @mcp.tool
    def start_planning_session(
        kitchen_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        FLUXO OFICIAL (passo 1). Cria uma sessao de planejamento persistida.

        kitchen_profile (opcional): fatos JA confirmados pela Dona Maria.
          Campos: equipment [{name, available, quantity?, capacity?,
          capacity_unit?}], equipment_inventory_complete (true somente se
          a lista de equipamentos for exaustiva), skills [..],
          daily_available_minutes, target_servings_per_day, available_people,
          gas_available, electricity_available, water_available,
          refrigerator_available, refrigerator_free_capacity_liters,
          freezer_available e freezer_free_capacity_liters. Omitir =
          desconhecido. Capacidade de armazenamento e espaco LIVRE em litros.
        """
        return _session_payload(service().start_session(kitchen_profile))

    @mcp.tool
    def update_kitchen_profile(
        session_id: str,
        expected_session_version: int,
        kitchen_profile: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Atualiza fatos da cozinha (merge por campo; null volta a ser desconhecido).
        equipment_inventory_complete=true somente se a lista de equipamentos
        estiver completa. Inclui refrigerator_available,
        refrigerator_free_capacity_liters, freezer_available e
        freezer_free_capacity_liters (espaco LIVRE em litros).
        Invalida avaliacoes e aprovacoes de TODOS os candidatos da sessao.
        """
        session, invalidated = service().update_kitchen_profile(
            session_id, expected_session_version, kitchen_profile
        )
        return {**_session_payload(session), "invalidated_candidates": invalidated}

    @mcp.tool
    def register_recipe_candidate(
        session_id: str,
        recipe: dict[str, Any],
        sources: list[dict[str, Any]],
        supplemental_quotes: list[dict[str, str]] | None = None,
        allow_partial_cmv: bool = False,
        candidate_id: str | None = None,
        expected_candidate_version: int | None = None,
        production_confirmation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Registra receita pesquisada via web_search (ou revisa: candidate_id +
        expected_candidate_version; a revisao invalida avaliacao e aprovacao).

        recipe: {recipe_id, name, description, servings, ingredients:
          [{name, quantity?, unit?, source_text?}], required_equipment?
          [{name, minimum_quantity?, minimum_capacity?, capacity_unit?}],
          required_skills?, required_utilities?, storage_requirements?
          [{kind: refrigerator|freezer, volume_liters_per_batch?, note?}],
          total_time_minutes? (por lote), minimum_people? e preparation_steps?.
          Requisitos omitidos = desconhecidos (nunca "nenhum"); use [] apenas
          quando confirmado que nao ha requisito. Use nomes exatos da despensa.
        sources: [{title, url, excerpt?, retrieved_at?, publisher?,
          manual_review? {reviewed_by, reviewed_at, note?}}]. Por padrao,
          ficam unverified_search_result. manual_review e uma declaracao
          registrada, nao uma verificacao automatica/autenticada.
        supplemental_quotes: [{name, package_quantity, package_unit,
          package_price, source}] com valores em string e source URL http(s).
        production_confirmation (opcional): {confirmed_batch_count,
          production_minutes?, user_confirmation}. So informe apos Dona Maria
          confirmar explicitamente o numero de lotes e, quando aplicavel,
          o tempo TOTAL de todos os lotes. Nao extrapole o tempo linearmente.
        allow_partial_cmv: true somente se Dona Maria aceitar ingredientes sem
          quantidade (ex.: sal a gosto); o CMV final sera marcado parcial.
        """
        candidate = service().register_candidate(
            session_id,
            recipe,
            sources,
            supplemental_quotes,
            allow_partial_cmv=allow_partial_cmv,
            candidate_id=candidate_id,
            expected_candidate_version=expected_candidate_version,
            production_confirmation=production_confirmation,
        )
        return _candidate_payload(candidate)

    @mcp.tool
    def evaluate_recipe_candidate(
        session_id: str,
        candidate_id: str,
        expected_candidate_version: int,
    ) -> dict[str, Any]:
        """
        Avalia de forma deterministica: equipamentos, habilidades, utilidades,
        tempo, despensa, ingredientes faltantes, cotacoes e orcamento
        compartilhado de R$ 80. Resultado: needs_information, infeasible ou
        awaiting_approval. Nao calcula CMV nem preco.
        """
        candidate = service().evaluate_candidate(
            session_id, candidate_id, expected_candidate_version
        )
        return _candidate_payload(candidate)

    @mcp.tool
    def approve_recipe_candidate(
        session_id: str,
        candidate_id: str,
        expected_candidate_version: int,
        decision: Literal["approve", "reject"],
        user_confirmation: str,
    ) -> dict[str, Any]:
        """
        Registra a decisao EXPLICITA da Dona Maria para a versao exata do
        candidato em awaiting_approval. user_confirmation deve citar a fala
        dela. Nunca chamar sem a confirmacao dela na conversa.
        """
        candidate = service().decide_candidate(
            session_id,
            candidate_id,
            expected_candidate_version,
            decision,
            user_confirmation,
        )
        return _candidate_payload(candidate)

    @mcp.tool
    def calculate_approved_recipe(
        session_id: str,
        candidate_id: str,
        expected_candidate_version: int,
        include_batch_pricing: bool = False,
    ) -> dict[str, Any]:
        """
        FLUXO OFICIAL de CMV e precificacao. Carrega o estado persistido,
        exige aprovacao explicita da versao atual e so entao usa os
        calculadores financeiros existentes (taxa de delivery de 10%).
        """
        candidate = service().calculate_approved(
            session_id,
            candidate_id,
            expected_candidate_version,
            include_batch_pricing=include_batch_pricing,
        )
        return _candidate_payload(candidate)

    @mcp.tool
    def get_planning_status(
        session_id: str,
        candidate_id: str | None = None,
    ) -> dict[str, Any]:
        """Retorna sessao, candidatos, status, versoes e eventos recentes."""
        session, candidates, events = service().get_status(session_id, candidate_id)
        return {
            **_session_payload(session),
            "candidates": [_candidate_payload(item) for item in candidates],
            "events": [_event_payload(item) for item in events],
        }
