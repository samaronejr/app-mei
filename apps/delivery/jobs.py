"""Read the registered beat schedule and distinguish completion from dispatch."""

from copy import copy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from celery.schedules import maybe_schedule
from django.apps import apps
from django.conf import settings
from django.utils import timezone

from apps.obligations.models import SchedulerHeartbeat
from config.celery import app as celery_app

# Compatibility with the heartbeat names already written by these shipped tasks.
# This is label/heartbeat metadata, never the inventory of scheduled work.
HEARTBEAT_NAMES: Final = {
    "generate_monthly_cycles": "generate_cycles",
    "record_scheduler_heartbeat": "beat",
    "ping_scanner": "scanner",
}
JOB_LABELS: Final = {
    "dispatch_outbox": "Envios da fila",
    "sweep_reminders": "Lembretes automáticos de documentos",
    "generate_monthly_cycles": "Geração de ciclos",
    "scan_quarantine": "Verificação de arquivos",
    "ping_scanner": "Verificador antivírus",
    "extract_documents": "Extração de documentos",
    "open_deterministic_exceptions": "Abertura de exceções",
    "purge_access_logs": "Retenção de registros de acesso",
    "materialize_receipt_observations": "Leitura de comprovantes",
    "record_scheduler_heartbeat": "Sinal do agendador",
    "refresh_das_calendars": "Atualização do calendário DAS",
    "record_usage": "Registro diário de uso",
    "schedule_payment_consultations": "Consulta mensal de pagamentos na Receita",
    "purge_payment_pages": "Retenção de páginas de pagamentos da Receita",
    "offboard_expired_engagements": "Encerramentos após o prazo",
    "queue_daily_summaries": "Resumo diário por e-mail",
    "dispatch_firm_outbox": "Envios de avisos do escritório",
    "sweep_account_exports": "Exportação da conta",
    "sweep_evidence_exports": "Exportação de evidências",
    "purge_expired_content": "Retenção de conteúdo",
    "resume_bulk_operations": "Operações em lote",
    "purge_agent_outputs": "Retenção de registros internos de processamento",
}
OUTCOME_LABELS: Final = {
    "evaluated": "Solicitações avaliadas em lotes concluídos",
    "queued": "Avisos incluídos em lotes concluídos",
    "existing": "Ocorrências já existentes",
    "skipped": "Solicitações ignoradas",
    "conflicts": "Conflitos de autorização ou conteúdo",
    "cancelled": "Avisos cancelados antes de iniciar",
    "failed_tenants": "Escritórios com falha",
}


@dataclass(frozen=True, slots=True)
class JobStatus:
    """A platform completion signal and a schedule estimate, not tenant success."""

    name: str
    label: str
    last_completed_at: datetime | None
    next_run_at: datetime | None
    enabled: bool
    due: bool
    last_attempt_at: datetime | None = None
    last_error_code: str = ""
    outcomes: tuple[tuple[str, int], ...] = ()
    # AXRM-005: the scanner job's last-known signature database version.
    signature_version: str = ""


def registered_jobs() -> tuple[JobStatus, ...]:
    """Honor persisted beat overrides without exposing task arguments or payloads."""
    definitions = settings.CELERY_BEAT_SCHEDULE
    periodic = apps.get_model("django_celery_beat", "PeriodicTask")
    persisted = {
        str(row.name): row
        for row in periodic.objects.filter(name__in=definitions).select_related(
            "interval", "crontab", "solar", "clocked"
        )
    }
    stamps = {stamp.name: stamp for stamp in SchedulerHeartbeat.objects.all()}
    now = timezone.now()
    result = []
    for name, definition in definitions.items():
        row = persisted.get(name)
        task = str(row.task) if row is not None else definition["task"]
        short_name = task.rsplit(".", 1)[-1]
        stamp = stamps.get(HEARTBEAT_NAMES.get(short_name, short_name))
        completed = (
            stamp.last_success_at
            if stamp is not None and stamp.last_attempt_at is not None
            else stamp.updated_at
            if stamp is not None
            else None
        )
        attempted = stamp.last_attempt_at if stamp is not None else None
        last_dispatched = row.last_run_at if row is not None else None
        enabled = bool(row.enabled) if row is not None else True
        starts = row.start_time if row is not None else None
        expires = row.expires if row is not None else None
        if row is not None and (
            (row.one_off and row.total_run_count > 0)
            or (expires is not None and expires <= now)
        ):
            enabled = False
        next_run = None
        due = False
        if enabled:
            schedule = (
                copy(row.schedule)
                if row is not None
                else maybe_schedule(copy(definition["schedule"]), app=celery_app)
            )
            local_now = now.astimezone(schedule.tz)
            schedule.nowfun = lambda instant=local_now: instant
            last_run = (last_dispatched or attempted or completed or now).astimezone(
                schedule.tz
            )
            remaining = schedule.remaining_estimate(last_run)
            due = remaining.total_seconds() <= 0
            next_run = now + max(remaining, timedelta(0))
            if starts is not None and starts > next_run:
                next_run, due = starts, False
            if expires is not None and next_run >= expires:
                next_run, enabled, due = None, False, False
        result.append(
            JobStatus(
                name=name,
                label=JOB_LABELS.get(short_name, short_name.replace("_", " ")),
                last_completed_at=completed,
                next_run_at=next_run,
                enabled=enabled,
                due=due,
                last_attempt_at=attempted,
                last_error_code=stamp.last_error_code if stamp else "",
                outcomes=tuple(
                    (label, int(stamp.outcomes_json[key]))
                    for key, label in OUTCOME_LABELS.items()
                    if stamp is not None and key in stamp.outcomes_json
                ),
                signature_version=str(
                    stamp.outcomes_json.get("signature_version", "") if stamp else ""
                ),
            )
        )
    return tuple(result)
